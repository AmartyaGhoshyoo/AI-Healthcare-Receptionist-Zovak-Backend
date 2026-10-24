"""Local-only GPT-Live client delegation over server-side WebSocket (sideband).

Browser owns WebRTC audio. Server sideband receives transcript/delegation events.
For production replace process-local session storage, unauthenticated routes,
partial-turn collection, and single-worker scheduling with durable services.
"""
import asyncio
import json
import logging
from secrets import compare_digest, token_urlsafe
from pathlib import Path
from uuid import uuid4

from websockets.asyncio.client import connect

from zovak.voice.transcripts import TranscriptBuffer
from zovak.voice.confirmation import (
    VoiceConfirmationGate, classify_confirmation, polite_confirmation_question,
)
from zovak.services.mock_scheduler import SchedulingError
from zovak.services.google_scheduler import describe_appointment
from zovak.voice.email_form import EmailFormGate, validate_typed_email

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
        self.email_form = EmailFormGate()
        # Generated per voice call. Do not use guessable conversation_id as auth.
        self.ui_token = token_urlsafe(32)
        self._socket = None
        self.needs_readback = False
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
                self._socket = socket
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
            self._socket = None
            self.ready.set()
            self.closed = True
            if self._on_close:
                self._on_close(self.session_id)

    async def email_state(self) -> dict:
        """Frontend polls this, and shows the input box only while awaiting."""
        async with self._agent_lock:
            pending = await self.scheduler.latest_pending("demo-business", self.conversation_id)
            if (not pending or self.email_form.proposal_id != pending.id
                    or not self.confirmation.proposal_id == pending.id):
                return {"state": "idle", "email_request": None}
            if self.email_form.verified:
                return {"state": "verified", "email_request": None}
            return {"state": "email_required", "email_request": {
                "type": "email_required",
                "request_id": self.email_form.request_id,
                "message": "Please enter your email address in the box on your screen.",
                "customer_name": pending.customer_name,
            }}

    async def submit_email(self, *, request_id: str, email: str) -> dict:
        """Backend-only typed email acceptance. NEVER confirms a booking."""
        if self.closed:
            raise SchedulingError("The voice session has ended")
        validated = validate_typed_email(email)
        async with self._agent_lock:
            pending = await self.scheduler.latest_pending("demo-business", self.conversation_id)
            gate = self.email_form
            if not pending or gate.proposal_id != pending.id or not gate.request_id:
                raise SchedulingError("No active email request for this call")
            if not compare_digest(gate.request_id, request_id):
                raise SchedulingError("The email request is stale or invalid")
            if gate.verified:
                if gate.verified_email != validated:
                    raise SchedulingError("Email was already submitted. Ask ZOVAK to change it.")
            else:
                await self.scheduler.set_proposal_email(
                    "demo-business", self.conversation_id, pending.id, validated
                )
                gate.verified_email = validated
            service = self.scheduler.services.get(pending.service_id)
            name = service.name if service else pending.service_id
            spoken_details = describe_appointment(pending, name).replace(
                f" Customer email: {pending.customer_email}.", " Email captured on screen."
            )
            message = (
                "Thanks, I received the email you entered on screen. "
                f"Please review these appointment details. {spoken_details} "
                "Nothing has been booked yet. Are you sure you'd like me to confirm "
                "this appointment and send the Google Calendar invitation? "
                "If so, say 'Yes, confirm my appointment'."
            )
            # Spoken UI update is best-effort: acknowledgement is not proof of
            # actual audio playback. Keep a fallback for the next voice turn.
            self.needs_readback = True
            if self._socket is not None:
                try:
                    await self._socket.send(json.dumps({
                        "type": "session.commentary.append",
                        "event_id": f"zovak_email_{uuid4().hex}",
                        "delegation_id": None,
                        "content": message,
                    }))
                    self.needs_readback = False
                except Exception:
                    logger.warning("Could not speak email-form completion; fallback next turn", exc_info=True)
            return {"state": "verified", "email": validated,
                    "appointment_summary": describe_appointment(pending, name),
                    "assistant_message": message, "booking_confirmed": False}

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
                self.email_form.clear()
                return (
                    "That proposal has expired or is no longer available. "
                    "I haven't booked anything. Let's choose another time."
                )

            # Voice web calls always require an on-screen typed email. Voice
            # transcripts never verify the email, even if they say "yes".
            if self.email_form.proposal_id != current.id:
                self.email_form.arm(current.id)
            if not self.email_form.verified:
                decision = classify_confirmation(text)
                if decision == "decline":
                    await self.scheduler.discard(tenant, self.conversation_id, armed_id)
                    self.confirmation.clear()
                    self.email_form.clear()
                    return "Of course, I cancelled that pending proposal. No booking was made."
                if decision == "revise":
                    await self.scheduler.discard(tenant, self.conversation_id, armed_id)
                    self.confirmation.clear()
                    self.email_form.clear()
                else:
                    return (
                        "Please enter your email address in the box on your screen "
                        "and press Confirm Email. I won't spell it aloud. "
                        "Nothing has been booked yet."
                    )
            elif self.email_form.verified_email != current.customer_email:
                # Proposal changed unexpectedly: fail closed.
                self.email_form.clear()
                self.confirmation.clear()
                return "The appointment details changed. Let's review a new proposal."
            elif self.needs_readback:
                # If the session-wide speak event wasn't sent successfully,
                # a distinct subsequent voice turn must read back first.
                self.needs_readback = False
                service = self.scheduler.services.get(current.service_id)
                name = service.name if service else current.service_id
                spoken_details = describe_appointment(current, name).replace(
                    f" Customer email: {current.customer_email}.", " Email captured on screen."
                )
                return (
                    f"Here are the appointment details. {spoken_details} "
                    "Nothing has been booked. Are you sure you'd like to confirm this "
                    "appointment and send the invitation?"
                )

            if (self.email_form.verified and "email" in text.casefold()
                    and any(w in text.casefold() for w in
                            ("wrong", "change", "incorrect", "correction", "mistake"))):
                self.email_form.clear()
                self.email_form.arm(current.id)
                self.needs_readback = False
                return "Sure, I reopened the email box. Please enter the correct address. No invitation has been sent."

            decision = classify_confirmation(text)
            if decision == "approve":
                # IMPORTANT: the ID comes from server state, NOT model output.
                try:
                    result = await self.scheduler.confirm(
                        tenant, self.conversation_id, armed_id
                    )
                except SchedulingError as exc:
                    still_pending = await self.scheduler.latest_pending(tenant, self.conversation_id)
                    if still_pending is None or still_pending.id != armed_id:
                        self.confirmation.clear()
                        self.email_form.clear()
                    return f"I could not confirm that appointment: {exc}. Nothing was booked. Please try again or choose another time."
                self.confirmation.clear()
                self.email_form.clear()
                logger.info("Voice booking confirmed by backend: %s", result.booking_id)
                service = self.scheduler.services.get(current.service_id)
                name = service.name if service else current.service_id
                details = describe_appointment(current, name).replace(
                    f" Customer email: {current.customer_email}.", " Email captured on screen."
                )
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
                self.email_form.clear()
                return (
                    "Of course. I haven't booked the appointment, and I've "
                    "removed that pending proposal. Would you like another time?"
                )

            if decision == "revise":
                # The customer changed their mind; disarm the old proposal
                # BEFORE processing a new request through the agent.
                await self.scheduler.discard(tenant, self.conversation_id, armed_id)
                self.confirmation.clear()
                self.email_form.clear()
            else:
                # A clarification about how the email is written is NOT a request
                # to abandon the prepared appointment. Still never treat a reply
                # containing extra qualifications as final booking consent.
                if current.customer_email and any(
                    word in text.casefold() for word in ("email", "gmail", "@")
                ):
                    return (
                        "Nothing is booked yet. If the email is incorrect, "
                        "please tell me to change it so I can reopen the email box. "
                        "Otherwise, say 'Confirm booking'."
                    )
                return (
                    "I still have your appointment proposal ready, but I haven't booked "
                    "anything yet. If all details are correct, please say "
                    "'Confirm booking' as one short sentence. Otherwise, "
                    "tell me what to change."
                )

        previous = await self.scheduler.latest_pending(tenant, self.conversation_id)
        agent_reply = await run_agent(text)
        latest = await self.scheduler.latest_pending(tenant, self.conversation_id)
        if latest is not None and (previous is None or latest.id != previous.id):
            self.confirmation.arm(latest.id)
            self.email_form.arm(latest.id)
            self.needs_readback = False
            return (
                "I've prepared your appointment. Please enter your email "
                "address in the box on your screen and press Confirm Email. "
                "I won't ask you to spell it. The appointment is not booked yet."
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
                            email_via_form=True,
                        )
                        result = await Runner.run(
                            make_receptionist(email_via_form=True), utterance, context=context, session=memory
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
