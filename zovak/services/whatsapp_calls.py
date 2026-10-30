"""WhatsApp voice calls answered by GPT-Live, through ZOVAK's audio bridge.

A direct Meta↔OpenAI connection can't work: both are ICE-lite servers (seen in
their SDPs: both a=ice-lite), so neither starts the connection checks. The
bridge (zovak/voice/webrtc_bridge.py) answers Meta's offer and calls GPT-Live
like a browser, copying audio between them. The usual sideband then runs the
call with the phone rules.
"""
from __future__ import annotations

import logging
from openai import APIError, AsyncOpenAI

from zovak.agents.receptionist import today_instructions
from zovak.api.routes.phone import PHONE_PROMPT, _too_many_calls
from zovak.voice.sideband import VoiceSideband
from zovak.voice.webrtc_bridge import AudioBridge

logger = logging.getLogger(__name__)

WHATSAPP_PROMPT = PHONE_PROMPT + (
    " This call comes in through WhatsApp. When the backend sends the booking form, it "
    "arrives as a WhatsApp message."
)


def sdp_summary(sdp: str) -> str:
    """The lines that decide whether two WebRTC endpoints can connect (for diagnosis)."""
    lines = [line.strip() for line in sdp.splitlines()]
    keep = [line for line in lines if line.startswith(
        ("m=", "a=ice-lite", "a=setup:", "a=ice-options", "a=group:", "a=sendrecv", "a=recvonly",
         "a=sendonly", "a=inactive", "a=crypto", "a=rtcp-mux"))]
    candidates = [line.split(" typ ")[1].split()[0] for line in lines
                  if line.startswith("a=candidate") and " typ " in line]
    keep.append(f"candidates={len(candidates)} types={sorted(set(candidates))}")
    keep.append(f"fingerprint={'yes' if any(l.startswith('a=fingerprint') for l in lines) else 'no'}")
    return " | ".join(keep)


class WhatsAppOrSms:
    """Send the booking form link as a WhatsApp message; fall back to the SMS router."""

    def __init__(self, cloud, sms):
        self.cloud, self.sms = cloud, sms

    async def send(self, to: str, body: str) -> None:
        try:
            await self.cloud.send_text(to.lstrip("+"), body)
            await self.sms.outbox.send(to, body)  # keep the test inbox copy
        except Exception:
            logger.warning("WhatsApp message failed; using SMS router", exc_info=True)
            await self.sms.send(to, body)


class WhatsAppCalls:
    def __init__(self, app_state, cloud, *, openai_client=None, bridge_factory=AudioBridge):
        self.state = app_state
        self.cloud = cloud
        self._openai = openai_client
        self._new_bridge = bridge_factory
        self.by_call: dict[str, VoiceSideband] = {}
        self.bridges: dict[str, AudioBridge] = {}

    def _client(self) -> AsyncOpenAI:
        return self._openai or AsyncOpenAI(api_key=self.state.settings.openai_api_key, max_retries=1)

    async def handle_payload(self, payload: dict) -> None:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("field") != "calls":
                    continue
                for call in change.get("value", {}).get("calls", []):
                    try:
                        if call.get("event") == "connect":
                            await self._connect(call)
                        elif call.get("event") == "terminate":
                            await self._terminated(call)
                    except Exception:
                        logger.exception("WhatsApp call event failed")

    async def _connect(self, call: dict) -> None:
        call_id, caller = call.get("id", ""), "+" + str(call.get("from", "")).lstrip("+")
        offer = (call.get("session") or {}).get("sdp")
        if not call_id or not offer or call_id in self.by_call:
            return
        settings = self.state.settings
        if _too_many_calls(caller, settings.max_calls_per_caller_per_hour):
            await self.cloud.call_action(call_id, "reject")
            return
        client = self._client()
        bridge = self._new_bridge(call_id[-10:])
        try:
            live = await client.live.create(
                session={"model": "gpt-live-1",
                         "instructions": WHATSAPP_PROMPT + " " + today_instructions(),
                         "delegation": {"type": "client"}},
                transport={"type": "webrtc", "sdp": await bridge.openai_offer()},
            )
            await bridge.openai_answer(live.transport.sdp)
            answer = await bridge.meta_answer(offer)
        except Exception:
            logger.exception("Could not set up the WhatsApp audio bridge")
            await bridge.close()
            await self.cloud.call_action(call_id, "reject")
            return
        logger.info("WhatsApp offer (Meta):  %s", sdp_summary(offer))
        logger.info("Bridge answer to Meta:  %s", sdp_summary(answer))
        await self.cloud.call_action(call_id, "pre_accept", answer)
        await self.cloud.call_action(call_id, "accept", answer)
        self.bridges[call_id] = bridge
        bridge.start_stats()
        logger.info("Accepted WhatsApp call %s from %s (GPT-Live session %s)",
                    call_id, caller, live.session.id)

        async def hang_up() -> None:
            try:
                await self.cloud.call_action(call_id, "terminate")
            finally:
                handler = self.by_call.get(call_id)
                if handler is not None and handler._socket is not None:
                    await handler._socket.send('{"type": "session.close"}')
                await self._close_bridge(call_id)

        # Same conversation as this person's WhatsApp chat (WhatsAppBot uses wa-<number>):
        # chat and calls share bookings and Nofa's memory. Meta verifies WhatsApp numbers,
        # unlike phone caller ID, so this link is safe.
        handler = VoiceSideband(
            session_id=live.session.id, conversation_id=f"wa-{caller.lstrip('+')}",
            api_key=settings.openai_api_key, scheduler=self.state.scheduler,
            agent_model=settings.openai_agent_model,
            on_close=lambda sid: self.by_call.pop(call_id, None),
            channel="phone", caller=caller, booking_forms=self.state.booking_forms,
            sms=WhatsAppOrSms(self.cloud, self.state.sms),
            public_base_url=settings.public_base_url or "http://127.0.0.1:8000",
            hang_up=hang_up,
        )
        self.by_call[call_id] = handler
        handler.start()

    async def _close_bridge(self, call_id: str) -> None:
        bridge = self.bridges.pop(call_id, None)
        if bridge is not None:
            logger.info("Bridge closed for %s: %s", call_id[-10:], bridge.stats)
            await bridge.close()

    async def _terminated(self, call: dict) -> None:
        await self._close_bridge(call.get("id", ""))
        handler = self.by_call.pop(call.get("id", ""), None)
        logger.info("WhatsApp call %s ended (%s)", call.get("id"), call.get("status"))
        if handler is not None and handler._socket is not None:
            try:
                await handler._socket.send('{"type": "session.close"}')
            except Exception:
                logger.debug("Session already closed", exc_info=True)
