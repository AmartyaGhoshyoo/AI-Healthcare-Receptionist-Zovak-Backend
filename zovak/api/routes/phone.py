"""Phone calls through GPT-Live SIP: OpenAI asks us to accept each incoming call.

Flow: a phone (or a SIP app like Linphone) calls OpenAI's SIP address for the
project → OpenAI sends `live.transport.incoming` here → we verify the signature,
accept the call with Nofa's phone instructions and attach the same sideband the
website call uses. Works the same for any SIP provider, so a real phone number
(e.g. a Bangladeshi one) plugs in later without code changes.
"""
from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict, deque
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from openai import APIError, AsyncOpenAI, InvalidWebhookSignatureError

from zovak.agents.receptionist import today_instructions
from zovak.providers.voice.gpt_live import LIVE_PROMPT
from zovak.voice.sideband import VoiceSideband

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/phone", tags=["phone (public webhook)"])

PHONE_PROMPT = LIVE_PROMPT + (
    " This is a PHONE call: the caller has no screen, so never mention buttons, boxes "
    "or the screen. Ask for the service, date, time and the caller's name. Never ask "
    "for an email address: the text-message form collects it. As soon as the call "
    "connects, greet the caller first: say you're Nofa from Brightside Family Clinic and "
    "ask how you can help with an appointment."
)

_seen_events: OrderedDict[str, None] = OrderedDict()  # webhook deliveries can repeat
_recent_calls: dict[str, deque[float]] = {}


def caller_id(sip_headers) -> str:
    """Caller from the SIP From header, e.g. '+8801712345678' or 'alice@sip.linphone.org'.

    Untrusted metadata: fine for addressing a text, NOT proof of identity.
    """
    value = next((h.value for h in sip_headers if h.name.lower() in ("from", "f")), "")
    match = re.search(r"<?sips?:([^@>;]+)(@[^>;]+)?", value)
    if not match:
        return "unknown"
    user, host = match.group(1), (match.group(2) or "")
    return user if re.fullmatch(r"\+?\d{6,15}", user) else f"{user}{host}"


def _too_many_calls(caller: str, limit: int) -> bool:
    now = time.monotonic()
    calls = _recent_calls.setdefault(caller, deque())
    while calls and now - calls[0] > 3600:
        calls.popleft()
    if len(calls) >= limit:
        return True
    calls.append(now)
    return False


def openai_client(api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key, max_retries=1)


@router.post("/openai-webhook")
async def openai_webhook(request: Request) -> dict:
    settings = request.app.state.settings
    if not settings.openai_webhook_secret or not settings.openai_api_key:
        raise HTTPException(503, detail="Set OPENAI_WEBHOOK_SECRET and OPENAI_API_KEY in .env")
    client = openai_client(settings.openai_api_key)
    try:
        event = client.webhooks.unwrap(
            await request.body(), request.headers, secret=settings.openai_webhook_secret,
        )
    except (InvalidWebhookSignatureError, ValueError) as exc:
        raise HTTPException(400, detail="Invalid webhook signature") from exc

    if event.id in _seen_events:
        return {"ok": True, "duplicate": True}
    _seen_events[event.id] = None
    while len(_seen_events) > 1000:
        _seen_events.popitem(last=False)
    if event.type != "live.transport.incoming":
        return {"ok": True, "ignored": event.type}

    session_id = event.data.session_id  # forward unchanged
    caller = caller_id(event.data.sip_headers)
    if _too_many_calls(caller, settings.max_calls_per_caller_per_hour):
        logger.warning("Rejecting call from %s: too many calls this hour", caller)
        await client.live.sessions.reject(session_id, status_code=486)  # busy here
        return {"ok": True, "rejected": "rate_limited"}

    try:
        await client.live.sessions.accept(session_id, session={
            "type": "live",
            "model": "gpt-live-1",
            "instructions": PHONE_PROMPT + " " + today_instructions(),
            "delegation": {"type": "client"},
        })
    except APIError:
        logger.exception("Could not accept phone call %s", session_id)
        return {"ok": True, "accepted": False}

    if not hasattr(request.app.state, "voice_sidebands"):
        request.app.state.voice_sidebands = {}
    active: dict = request.app.state.voice_sidebands

    async def hang_up() -> None:
        await openai_client(settings.openai_api_key).live.sessions.hangup(session_id)

    handler = VoiceSideband(
        session_id=session_id,
        conversation_id=f"phone-{uuid4().hex}",
        api_key=settings.openai_api_key,
        scheduler=request.app.state.scheduler,
        agent_model=settings.openai_agent_model,
        on_close=lambda sid: active.pop(sid, None),
        channel="phone",
        caller=caller,
        booking_forms=request.app.state.booking_forms,
        sms=request.app.state.sms,
        public_base_url=settings.public_base_url or "http://127.0.0.1:8000",
        hang_up=hang_up,
    )
    active[session_id] = handler
    handler.start()
    logger.info("Accepted phone call %s from %s (call audio: %s)", session_id, caller,
                event.data.sip_media_security or "unknown")
    if event.data.sip_media_security == "rtp":
        logger.warning("Call audio is NOT encrypted (rtp); GPT-Live needs SRTP, expect no sound")
    return {"ok": True, "accepted": True}
