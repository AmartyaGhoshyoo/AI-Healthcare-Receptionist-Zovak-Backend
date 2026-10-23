"""Local-only GPT-Live client delegation over server-side WebSocket (sideband).

Browser owns WebRTC audio. Server sideband receives transcript/delegation events.
For production replace process-local session storage, unauthenticated routes,
partial-turn collection, and single-worker scheduling with durable services.
"""
import asyncio
import json
import logging
from pathlib import Path
from uuid import uuid4
from zovak.voice.confirmation import appointment_details
from websockets.asyncio.client import connect

from zovak.voice.transcripts import TranscriptBuffer
from zovak.voice.confirmation import (
    VoiceConfirmationGate, classify_confirmation, polite_confirmation_question,
)
from zovak.services.mock_scheduler import SchedulingError
from zovak.services.google_scheduler import describe_appointment

logger = logging.getLogger(__name__)


class VoiceSideband:
    def __init__(self, *, session_id: str, conversation_id: str, api_key: str,
                 scheduler, agent_model: str, on_close=None):
        self.session_id = session_id
        self.conversation_id = conversation_id
        self.api_key = api_key
        self.scheduler = scheduler
        self.agent_model = agent_model
        self.transcripts = TranscriptBuffer()
        self.ready = asyncio.Event()
        self.failed: str | None = None
        self.closed = False
        self._task: asyncio.Task | None = None
        self._agent_lock = asyncio.Lock()
        self.confirmation = VoiceConfirmationGate()
        self._pending: set[asyncio.Task] = set()
        self._delegations: set[str] = set()
        self._on_close = on_close

    def start(self) -> None:
        self._task = asyncio.create_task(self._listen(), name=f"zovak-voice-{self.session_id}")

    async def stop(self) -> None:
        self.closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        for task in list(self._pending):
            task.cancel()
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)

    async def _listen(self) -> None:
        url = f"wss://api.openai.com/v1/live/sessions/{self.session_id}/attach"
        try:
            async with connect(
                url,
                additional_headers={"Authorization": f"Bearer {self.api_key}"},
                max_size=8 * 1024 * 1024,
                ping_interval=20,
                open_timeout=10,
            ) as socket:
                self.ready.set()
                logger.info("GPT-Live sideband connected for conversation %s", self.conversation_id)
                async for raw in socket:
                    event = json.loads(raw)
                    kind = event.get("type", "")
                    if kind == "session.input_transcript.delta":
                        self.transcripts.ingest(event)
                    elif kind == "session.delegation.created":
                        delegation = event.get("delegation") or {}
                        delegation_id = delegation.get("id")
                        if delegation.get("target") != "client" or not delegation_id:
                            continue
                        if delegation_id in self._delegations:
                            continue
                        self._delegations.add(delegation_id)
                        task = asyncio.create_task(self._delegate(socket, delegation_id, int(event.get("offset_ms", 0))))
                        self._pending.add(task)
                        task.add_done_callback(self._pending.discard)
                    elif kind == "session.closed":
                        logger.info("GPT-Live session ended: %s", self.session_id)
                        return
                    elif kind == "error" or kind == "session.error":
                        logger.warning("GPT-Live session error: %s", str(event.get("error", event))[:400])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failed = str(exc)
            logger.exception("GPT-Live sideband connection failed")
        finally:
            self.ready.set()
            self.closed = True
            if self._on_close:
                self._on_close(self.session_id)

    async def _handle_turn(self, text: str, run_agent) -> str:
        """Coordinate voice confirmation without giving the LLM booking authority.

        Serialized by _agent_lock in _delegate. Uses the scheduler's trusted
        proposal records rather than an LLM-supplied proposal ID.
        """
        tenant = "demo-business"  # Local-only, hardcoded demo identity.
        current = await self.scheduler.latest_pending(tenant, self.conversation_id)
        armed_id = self.confirmation.proposal_id

        if armed_id:
            if current is None or current.id != armed_id:
                self.confirmation.clear()
                return (
                    "That proposal has expired or is no longer available. "
                    "I haven't booked anything. Let's choose another time."
                )

            decision = classify_confirmation(text)
            if decision == "approve":
                # IMPORTANT: the ID comes from server state, NOT model output.
                try:
                    result = await self.scheduler.confirm(
                        tenant, self.conversation_id, armed_id
                    )
                except SchedulingError as exc:
                    self.confirmation.clear()
                    return f"I could not confirm that appointment: {exc}. Nothing was booked."
                self.confirmation.clear()
                logger.info("Voice booking confirmed by backend: %s", result.booking_id)
                service = self.scheduler.services.get(current.service_id)
                name = service.name if service else current.service_id
                details = describe_appointment(current, name)
                if result.provider == "google-calendar":
                    return (
                        f"Your appointment is now confirmed. Google Calendar created the appointment. "
                        f"{details} Booking reference: {result.booking_id}. "
                        "Google Calendar was asked to email the invitation. "
                        "Please check your inbox and accept it if needed."
                    )
                return (
                    f"Your demo appointment is now confirmed. {details} "
                    f"Booking reference: {result.booking_id}. This was not added to Google Calendar."
                )

            if decision == "decline":
                await self.scheduler.discard(tenant, self.conversation_id, armed_id)
                self.confirmation.clear()
                return (
                    "Of course. I haven't booked the appointment, and I've "
                    "removed that pending proposal. Would you like another time?"
                )

            if decision == "revise":
                # The customer changed their mind; disarm the old proposal
                # BEFORE processing a new request through the agent.
                await self.scheduler.discard(tenant, self.conversation_id, armed_id)
                self.confirmation.clear()
            else:
                return (
                    "No problem. I haven't booked anything yet. "
                    "To approve this exact appointment, please say, "
                    "'Yes, confirm my appointment.' Or tell me what you'd like to change."
                )

        previous = await self.scheduler.latest_pending(tenant, self.conversation_id)
        agent_reply = await run_agent(text)
        latest = await self.scheduler.latest_pending(tenant, self.conversation_id)
        if latest is not None and (previous is None or latest.id != previous.id):
            self.confirmation.arm(latest.id)
            service = self.scheduler.services.get(latest.service_id)
            service_name = service.name if service else latest.service_id
            # This is generated from trusted backend state, not LLM prose.
            return (
                "Here are the details before I book anything. "
                f"{describe_appointment(latest, service_name)} "
                "Nothing is booked yet. Are you sure you want me to confirm this exact "
                "appointment and send a Calendar invitation to the email provided? "
                "If so, please say, 'Yes, confirm my appointment.'"
            )
        return agent_reply

    async def _delegate(self, socket, delegation_id: str, offset_ms: int) -> None:
        # A short grace period for transcript fragments; this is NOT a
        # production-grade finished-turn signal.
        await asyncio.sleep(0.6)
        text = self.transcripts.take_for_delegation(offset_ms)
        if not text:
            reply = "I didn't catch the full request. Please repeat it."
        else:
            async with self._agent_lock:
                try:
                    from agents import Runner
                    from agents.extensions.memory import AsyncSQLiteSession
                    from zovak.agents.context import AgentContext
                    from zovak.agents.receptionist import make_receptionist

                    async def run_agent(utterance: str) -> str:
                        Path('.data').mkdir(exist_ok=True)
                        memory = AsyncSQLiteSession(
                            f"demo-business:{self.conversation_id}",
                            db_path=".data/dev_conversations.db",
                        )
                        context = AgentContext(
                            tenant_id="demo-business",
                            conversation_id=self.conversation_id,
                            scheduler=self.scheduler,
                        )
                        result = await Runner.run(
                            make_receptionist(), utterance, context=context, session=memory
                        )
                        return str(result.final_output)

                    reply = await self._handle_turn(text, run_agent)
                    logger.info("Backend completed voice delegation %s", delegation_id)
                except Exception:
                    logger.exception("Voice delegated agent or confirmation failed")
                    reply = "Sorry, I couldn't complete that request. Please try again."
        if not self.closed:
            try:
                await socket.send(json.dumps({
                    "type": "session.commentary.append",
                    "event_id": f"zovak_{uuid4().hex}",
                    "delegation_id": delegation_id,
                    "content": reply,
                }))
            except Exception:
                logger.exception("Could not return GPT-Live delegation result")
