"""Local-only GPT-Live client delegation over server-side WebSocket (sideband).

Browser owns WebRTC audio. Server sideband receives transcript/delegation events.
For production replace process-local session storage, unauthenticated routes,
partial-turn collection, and single-worker scheduling with durable services.
"""
import asyncio
import json
import logging
from secrets import compare_digest, token_urlsafe
from datetime import datetime
from time import monotonic
from uuid import uuid4

from websockets.asyncio.client import connect

from zovak.voice.transcripts import TranscriptBuffer
from zovak.voice.confirmation import (
    VoiceConfirmationGate, classify_change_reply, classify_confirmation, is_done, is_goodbye,
    is_meaningful,
)
from zovak.agents.memory import memory_session, note_for_agent
from zovak.services.booking_state import booking_state, booking_status_for_agent, free_days, slot_request
from zovak.services.doctor_choice import read_choices, wants_new_email
from zovak.services.mock_scheduler import SchedulingError
from zovak.services.google_scheduler import describe_appointment
from zovak.voice.email_form import EmailFormGate, validate_typed_email
from zovak.voice.call_guard import CallGuard, speech_seconds

_TENANT = "demo-business"  # Local-only, hardcoded demo identity.

_CHECK_IN = (
    "Are you still there? If I don't hear anything in the next 10 seconds, I'll end "
    "the call, and you won't be able to say anything after that."
)
_GOODBYE = {
    "silence": (
        "I haven't heard from you, so I'll end the call now. Nothing has changed "
        "with your appointment. Goodbye!"
    ),
    "off_topic": (
        "I'm only able to help with appointments, so I'll end the call now. "
        "Feel free to call back any time to book. Goodbye!"
    ),
    "done": "Thanks for calling. I'll end the call now. Goodbye!",
    "misuse": (
        "I've noticed a lot of changes on this call. To avoid mistakes, I'll end the "
        "call now. Please call back when you're ready. Goodbye!"
    ),
    "noise": (
        "I'm not hearing a request, so I'll end the call now. Please call back "
        "whenever you're ready to book. Goodbye!"
    ),
}
_THANKS = "Thanks for calling Brightside Family Clinic. Take care, goodbye!"
# Leaving with something half-done: ask once, a second goodbye ends the call.
_LEAVE_PROPOSAL = (
    "Before you go, your appointment isn't booked yet. Would you like to confirm it, "
    "or leave it for now?"
)
_LEAVE_CHANGE = (
    "Before you go, that change isn't done yet. Would you like to confirm it, or keep "
    "your appointment as it is?"
)
_DIDNT_CATCH = "Sorry, I didn't catch that. How can I help you with an appointment?"
_ANYTHING_ELSE = " Is there anything else I can help with?"
_GREETING = "Hi, this is Nofa from Brightside Family Clinic. How can I help you with an appointment today?"
_NO_AUDIO = 45  # seconds without hearing the caller at all: the audio isn't connected
# A "yes" that more speech followed may be the start of "yes, but...": never act on it.
_NOT_SETTLED = "Sorry, I didn't catch all of that. Nothing has changed yet. What would you like to do?"
_FORM_SENT = (
    "I've sent you a text message with a short form. Please add your email and confirm "
    "within 2 minutes. If you want to change the slot or cancel the booking, you can "
    "still do it from the form. I'll end the call now. Goodbye!"
)
_OFF_TOPIC_REPLY = {
    1: "Sorry, I can only help with appointments: booking, moving or cancelling one. Would you like to book an appointment?",
    2: "I can only help with appointments. If there's nothing to book, move or cancel, I'll end the call.",
}

logger = logging.getLogger(__name__)


class VoiceSideband:
    HANGUP_GRACE = 4.0  # seconds for the website to see call_ended before we close
    SETTLE_SECONDS = 1.5  # quiet time required before acting on a spoken "yes"

    def __init__(self, *, session_id: str, conversation_id: str, api_key: str,
                 scheduler, agent_model: str, on_close=None, channel: str = "web",
                 caller: str | None = None, booking_forms=None, sms=None,
                 public_base_url: str = "", hang_up=None):
        """channel: "web" (website call with a screen) or "phone" (SIP call: the
        booking is confirmed through a form sent by SMS)."""
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
        # Silence and off-topic limits; call_ended tells the website to hang up.
        self.guard = CallGuard()
        self.call_ended: str | None = None
        self._watchdog: asyncio.Task | None = None
        self.channel = channel
        self.caller = caller
        self.booking_forms = booking_forms
        self.sms = sms
        self.public_base_url = public_base_url
        self._hang_up = hang_up
        self.wrap_up = False  # an action just finished; "no, that's all" ends the call
        self._end_requested: str | None = None  # set by the agent's end_call / report_misuse
        self._leave_warned = False  # asked "before you go…" about a half-done booking
        self._screen_email: str | None = None  # last email typed on screen in this call
        self._last_heard_at = 0.0  # last transcript fragment, for the goodbye fallback
        self._goodbye_checked_ms = -1
        self._turn_heard_at: float | None = None  # when this turn's words were taken
        self._last_input_at = 0.0  # when the caller last said anything
        # The silence timer only runs once the caller's audio has actually arrived.
        self._heard_caller = False
        self._nofa_spoke = False
        self._connected_at = 0.0

    def start(self) -> None:
        self._task = asyncio.create_task(self._listen(), name=f"zovak-voice-{self.session_id}")

    async def stop(self) -> None:
        self.closed = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._watchdog is not None:
            self._watchdog.cancel()
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
                self._connected_at = monotonic()
                self.ready.set()
                self._watchdog = asyncio.create_task(self._watch_silence())
                logger.info("GPT-Live sideband connected for %s call %s",
                            self.channel, self.conversation_id)
                if self.channel == "phone":
                    await self._speak(_GREETING)  # if you hear this, audio to the phone works
                async for raw in socket:
                    event = json.loads(raw)
                    kind = event.get("type", "")
                    if self.guard.ending and kind in (
                        "session.input_transcript.delta", "session.delegation.created",
                    ):
                        continue  # Nofa said goodbye: nothing the caller says counts now
                    if kind == "session.input_transcript.delta":
                        if not self._heard_caller:
                            self._heard_caller = True
                            logger.info("Caller audio is arriving (%.1f s after connect)",
                                        monotonic() - self._connected_at)
                        self.transcripts.ingest(event)
                        self._caller_said(str(event.get("delta") or ""))
                        self._watch_for_goodbye()
                    elif kind == "session.output_transcript.delta":
                        if not self._nofa_spoke:
                            self._nofa_spoke = True
                            logger.info("Nofa is speaking (%.1f s after connect)",
                                        monotonic() - self._connected_at)
                        self.guard.agent_speaking(monotonic(), 1.5)
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
            if self._watchdog is not None:
                self._watchdog.cancel()
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
        self.guard.caller_active()
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
                self._screen_email = validated
                await note_for_agent(_TENANT, self.conversation_id,
                                     "The customer entered their email on screen for the pending appointment.")
            service = self.scheduler.services.get(pending.service_id)
            name = service.name if service else pending.service_id
            spoken_details = describe_appointment(pending, name).replace(
                f" Customer email: {pending.customer_email}.", " Email captured on screen."
            )
            message = (
                "Thanks, I received the email you entered on screen. "
                f"Please review these appointment details. {spoken_details} "
                "Nothing has been booked yet. Are you sure you'd like me to confirm it? "
                "If so, tap Confirm booking on your screen, or say 'Yes, confirm my appointment'."
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

    # ── Website buttons ──────────────────────────────────────────────────────
    # Each action re-checks trusted server state under the agent lock, then
    # speaks the verified result on the live call (best effort).

    async def _speak(self, message: str) -> None:
        self.guard.agent_speaking(monotonic(), speech_seconds(message))
        if self._socket is None:
            return
        try:
            await self._socket.send(json.dumps({
                "type": "session.commentary.append",
                "event_id": f"zovak_ui_{uuid4().hex}",
                "delegation_id": None,
                "content": message,
            }))
        except Exception:
            logger.warning("Could not speak button result on the call", exc_info=True)

    # ── Ending the call (silence / off-topic) ────────────────────────────────

    def _arm_email(self, proposal) -> None:
        """Email box for a proposal; skipped when it carries the email typed on screen earlier."""
        trusted = proposal.customer_email if (
            proposal.customer_email and proposal.customer_email == self._screen_email) else None
        self.email_form.arm(proposal.id, trusted_email=trusted)

    def _email_open(self) -> bool:
        armed = self.confirmation.proposal_id
        return bool(armed and self.email_form.proposal_id == armed and not self.email_form.verified)

    async def _watch_silence(self) -> None:
        while not self.closed and self.call_ended is None:
            await asyncio.sleep(1)
            if not self._heard_caller:
                # No caller audio yet: a broken audio path must not look like silence.
                if self._connected_at and monotonic() - self._connected_at > _NO_AUDIO:
                    logger.warning(
                        "No caller audio in %d s (Nofa %s). Check SRTP/network on the caller side.",
                        _NO_AUDIO, "did speak" if self._nofa_spoke else "never spoke",
                    )
                    await self._finish_call("no_audio", "")
                    return
                continue
            action = self.guard.tick(
                1.0, monotonic(), busy=self._agent_lock.locked(), email_open=self._email_open(),
            )
            if action == "check_in":
                await self._speak(_CHECK_IN)
            elif action == "end":
                await self._speak(_GOODBYE["silence"])
                await self._finish_call("silence", _GOODBYE["silence"])
                return

    async def _finish_call(self, reason: str, goodbye: str) -> None:
        """Wait for the goodbye to be heard, then tell the website to hang up."""
        self.guard.ending = True
        await asyncio.sleep(speech_seconds(goodbye))
        self.call_ended = reason
        logger.info("Ending voice call %s: %s", self.conversation_id, reason)
        if self._hang_up is not None:  # phone: hang up the SIP call
            try:
                await self._hang_up()
            except Exception:
                logger.warning("Could not hang up phone call %s", self.session_id, exc_info=True)
            return
        # Give the website a couple of polls to see call_ended and hang up itself.
        await asyncio.sleep(self.HANGUP_GRACE)
        if self._socket is not None and not self.closed:
            try:  # best effort; the website also closes its side
                await self._socket.send(json.dumps({"type": "session.close"}))
            except Exception:
                logger.warning("Could not close GPT-Live session from the backend", exc_info=True)

    def _caller_said(self, words: str) -> None:
        """Only real words count as the caller being there: a cough, "uh" or "hmm"
        doesn't reset the silence timer (someone pretending to answer)."""
        if is_meaningful(words):
            self.guard.caller_active()
            self._last_input_at = monotonic()

    # ── Goodbyes ─────────────────────────────────────────────────────────────

    GOODBYE_WAIT = 2.0  # seconds of quiet before checking a turn ZOVAK wasn't asked about

    def _watch_for_goodbye(self) -> None:
        self._last_heard_at = monotonic()
        task = asyncio.create_task(self._check_goodbye())
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _check_goodbye(self) -> None:
        """Fallback: the voice model answered "bye" itself instead of asking ZOVAK.

        Runs after each fragment; only the one after the caller's last words acts.
        Words already handed to ZOVAK by a delegation are skipped.
        """
        await asyncio.sleep(self.GOODBYE_WAIT)
        if (monotonic() - self._last_heard_at < self.GOODBYE_WAIT or self.guard.ending
                or self.closed or self._agent_lock.locked()):
            return
        fragments = self.transcripts.input_fragments
        since = max(self.transcripts.last_delegation_ms, self._goodbye_checked_ms)
        text = "".join(words for end, words in fragments if end > since).strip()
        if fragments:
            self._goodbye_checked_ms = fragments[-1][0]
        if not is_goodbye(text):
            return
        async with self._agent_lock:
            if self.guard.ending:
                return
            self.transcripts.last_delegation_ms = max(self.transcripts.last_delegation_ms,
                                                      self._goodbye_checked_ms)
            reply = await self._goodbye_turn()
        logger.info("Caller said goodbye (not delegated): %r", text[:80])
        await self._speak(reply)

    async def _goodbye_turn(self) -> str:
        """The caller wants to finish: a warm goodbye, unless something is half-done."""
        pending = await self.scheduler.latest_pending(_TENANT, self.conversation_id)
        change = await self.scheduler.latest_change(_TENANT, self.conversation_id)
        if (pending or change) and not self._leave_warned:
            self._leave_warned = True
            return _LEAVE_PROPOSAL if pending else _LEAVE_CHANGE
        if pending:
            await self.scheduler.discard(_TENANT, self.conversation_id, pending.id)
            self.confirmation.clear()
            self.email_form.clear()
            return self._end("done", "No problem, I haven't booked anything. " + _THANKS)
        if change:
            await self.scheduler.discard_change(_TENANT, self.conversation_id, change.id)
            return self._end("done", "No problem, your appointment stays as it was. " + _THANKS)
        return self._end("done", await self._farewell())

    async def _farewell(self) -> str:
        """ "You're all set with Dr. Rahman on Friday at 11:30 AM. Thanks for calling…" """
        booking = await self.scheduler.latest_booking(_TENANT, self.conversation_id)
        if booking is None:
            return _THANKS
        start = booking.start.astimezone(self.scheduler.timezone)
        days = (start.date() - datetime.now(self.scheduler.timezone).date()).days
        day = {0: "today", 1: "tomorrow"}.get(days, f"on {start:%A, %B} {start.day}")
        doctor = self.scheduler.doctors.get(booking.doctor_id or "")
        with_doctor = f" with {doctor.name}" if doctor else ""
        time_of_day = start.strftime("%I:%M %p").lstrip("0")
        return f"You're all set{with_doctor} {day} at {time_of_day}. {_THANKS}"

    def _noise_only_turn(self) -> str:
        """Reply to a turn with no real words, without involving the agent."""
        if self.guard.noise_turn():
            logger.info("Ending call %s: noise only, no request", self.conversation_id)
            return self._end("noise")
        return _DIDNT_CATCH

    def _end(self, reason: str, message: str | None = None) -> str:
        """Say goodbye (returned as this turn's reply), then end the call."""
        goodbye = message or _GOODBYE[reason]
        self.guard.ending = True
        task = asyncio.create_task(self._finish_call(reason, goodbye))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return goodbye

    async def _settled(self) -> bool:
        """Wait for a moment of silence before acting on a spoken "yes".

        If the caller kept talking ("yes... but change the time"), it wasn't a
        complete yes: the new words arrive as their own turn instead.
        """
        heard = self._turn_heard_at
        if heard is None:  # not a live call (tests, direct calls)
            return True
        deadline = monotonic() + 4
        while monotonic() < deadline:
            if self._last_input_at > heard:
                return False
            if monotonic() - heard >= self.SETTLE_SECONDS:
                return True
            await asyncio.sleep(0.1)
        return self._last_input_at <= heard

    def request_end(self) -> None:
        """The agent's end_call tool: the caller is finished."""
        self._end_requested = self._end_requested or "done"

    def report_misuse(self) -> None:
        """The agent's report_misuse tool: the caller is clearly wasting time."""
        self._end_requested = "misuse"

    def report_off_topic(self) -> int:
        """Called by the agent's report_off_topic tool; counts strikes for this call."""
        return self.guard.strike()

    def _off_topic_reply(self, strikes_before: int) -> str | None:
        """Fixed reply when the agent flagged this turn as off-topic."""
        if self.guard.off_topic == strikes_before:
            return None
        if self.guard.out_of_strikes:
            return self._end("off_topic")
        return _OFF_TOPIC_REPLY[self.guard.off_topic]

    def _armed_proposal(self, pending):
        if pending is None or self.confirmation.proposal_id != pending.id:
            return None
        return pending

    async def booking_state(self) -> dict:
        """What the voice panel should show; the website polls this during the call."""
        async with self._agent_lock:
            pending = self._armed_proposal(
                await self.scheduler.latest_pending(_TENANT, self.conversation_id)
            )
            if pending is not None and self.email_form.proposal_id != pending.id:
                self._arm_email(pending)
            email_ready = bool(
                pending and self.email_form.verified
                and self.email_form.verified_email == pending.customer_email
            )
            state = await booking_state(
                self.scheduler, _TENANT, self.conversation_id, proposal=pending,
                email_ready=email_ready, email_request_id=self.email_form.request_id,
            )
            return {**state, "call_ended": self.call_ended, "ending": self.guard.ending}

    async def confirm_booking(self, *, proposal_id: str) -> dict:
        """'Confirm booking' button. Same checks as a spoken approval."""
        self.guard.caller_active()
        if self.closed:
            raise SchedulingError("The voice session has ended")
        async with self._agent_lock:
            current = self._armed_proposal(
                await self.scheduler.latest_pending(_TENANT, self.conversation_id)
            )
            if current is None or not compare_digest(current.id, proposal_id):
                raise SchedulingError("This appointment is no longer waiting for confirmation")
            if (not self.email_form.verified
                    or self.email_form.verified_email != current.customer_email):
                raise SchedulingError("Please enter your email first")
            message = await self._commit_booking(current)
        await self._speak(message)
        return {"message": message}

    async def reopen_email(self, *, proposal_id: str) -> dict:
        """'Change email' link: show the email box again for the same appointment."""
        self.guard.caller_active()
        async with self._agent_lock:
            current = self._armed_proposal(
                await self.scheduler.latest_pending(_TENANT, self.conversation_id)
            )
            if current is None or not compare_digest(current.id, proposal_id):
                raise SchedulingError("This appointment is no longer waiting for confirmation")
            self.email_form.clear()
            self._screen_email = None  # the patient wants a different email: ask again
            self._arm_email(current)
            self.needs_readback = False
        return {"message": "Email box reopened."}

    async def confirm_change(self, *, change_id: str) -> dict:
        """'Confirm move' / 'Cancel appointment' buttons."""
        self.guard.caller_active()
        if self.closed:
            raise SchedulingError("The voice session has ended")
        async with self._agent_lock:
            change = await self.scheduler.latest_change(_TENANT, self.conversation_id)
            if change is None or not compare_digest(change.id, change_id):
                raise SchedulingError("This change is no longer waiting for confirmation")
            message = await self._commit_change(change)
        await self._speak(message)
        return {"message": message}

    async def discard_change(self, *, change_id: str) -> dict:
        """'Keep it' button: drop the pending move/cancel; the booking is unchanged."""
        self.guard.caller_active()
        async with self._agent_lock:
            change = await self.scheduler.latest_change(_TENANT, self.conversation_id)
            if change is None or not compare_digest(change.id, change_id):
                raise SchedulingError("This change is no longer waiting for confirmation")
            await self.scheduler.discard_change(_TENANT, self.conversation_id, change.id)
        await note_for_agent(_TENANT, self.conversation_id,
                             "The customer chose to keep the appointment unchanged.")
        self.wrap_up = True
        message = "No problem, I've kept your appointment exactly as it was." + _ANYTHING_ELSE
        await self._speak(message)
        return {"message": message}

    async def _commit_booking(self, current) -> str:
        """Confirm the armed proposal. Caller holds the lock and has checked the email."""
        try:
            result = await self.scheduler.confirm(_TENANT, self.conversation_id, current.id)
        except SchedulingError as exc:
            still_pending = await self.scheduler.latest_pending(_TENANT, self.conversation_id)
            if still_pending is None or still_pending.id != current.id:
                self.confirmation.clear()
                self.email_form.clear()
            return f"I could not confirm that appointment: {exc}. Nothing was booked. Please try again or choose another time."
        self.confirmation.clear()
        self.email_form.clear()
        self.wrap_up = True
        logger.info("Voice booking confirmed by backend: %s", result.booking_id)
        await note_for_agent(_TENANT, self.conversation_id,
                             "The customer confirmed the booking. The appointment is now CONFIRMED.")
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
                "Please check your inbox and accept it if needed." + _ANYTHING_ELSE
            )
        return (
            f"Your demo appointment is now confirmed. {details} "
            f"Booking reference: {result.booking_id}. This was not added to Google Calendar."
            + _ANYTHING_ELSE
        )

    async def _commit_change(self, change) -> str:
        """Apply a pending move/cancel. Caller holds the lock."""
        try:
            result = await self.scheduler.apply_change(_TENANT, self.conversation_id, change.id)
        except SchedulingError as exc:
            return f"I couldn't complete that change: {exc}. Your appointment is unchanged."
        self.wrap_up = True
        await note_for_agent(_TENANT, self.conversation_id, (
            "The customer confirmed the move; the booking is now at the new time."
            if result.status == "moved" else
            "The customer confirmed the cancellation; the booking is cancelled."
        ))
        if result.status == "cancelled":
            return (
                "Your appointment is cancelled. "
                + ("Google Calendar was asked to email the cancellation. "
                   if result.provider == "google-calendar" else "")
                + _ANYTHING_ELSE.strip()
            )
        when = change.new_start.astimezone(self.scheduler.timezone)
        return (
            f"Done. Your appointment is moved to {when.strftime('%A, %B %d at %I:%M %p')}, "
            "Dhaka time. "
            + ("Google Calendar was asked to email the updated invitation."
               if result.provider == "google-calendar" else "")
            + _ANYTHING_ELSE
        )

    def _change_question(self, change, booking) -> str:
        if change.kind == "move":
            old = booking.start.astimezone(self.scheduler.timezone)
            new = change.new_start.astimezone(self.scheduler.timezone)
            return (
                f"I can move your appointment from {old.strftime('%A, %B %d at %I:%M %p')} "
                f"to {new.strftime('%A, %B %d at %I:%M %p')}, Dhaka time. Nothing has "
                "changed yet. Tap Confirm move on your screen, or say 'Yes, confirm the move.'"
            )
        start = booking.start.astimezone(self.scheduler.timezone)
        return (
            f"Just to be sure: cancel your appointment on "
            f"{start.strftime('%A, %B %d at %I:%M %p')}? Nothing is cancelled yet. "
            "Tap Cancel appointment on your screen, or say 'Yes, cancel it.'"
        )

    # ── Phone: confirm through the SMS form ──────────────────────────────────

    def _phone_question(self, proposal) -> str:
        service = self.scheduler.services.get(proposal.service_id)
        when = proposal.start.astimezone(self.scheduler.timezone)
        return (
            f"I have a {service.name if service else proposal.service_id} for "
            f"{proposal.customer_name} on {when.strftime('%A, %B %d at %I:%M %p')}, Dhaka time. "
            "If that's right, say 'confirm' and I'll text you a short form to finish the "
            "booking. Nothing is booked yet."
        )

    async def _send_form(self, proposal) -> str:
        """Spoken "confirm" on a call sends the form; submitting it is what books."""
        form = await self.booking_forms.create(_TENANT, proposal, self.caller or "unknown")
        link = f"{self.public_base_url.rstrip('/')}/form/{form.token}"
        service = self.scheduler.services.get(proposal.service_id)
        when = proposal.start.astimezone(self.scheduler.timezone)
        await self.sms.send(self.caller or "unknown", (
            f"Nofa: confirm your {service.name if service else proposal.service_id} on "
            f"{when.strftime('%a %b %d, %I:%M %p')} within 2 minutes: {link} "
            "You can also pick another time or cancel there."
        ))
        self.confirmation.clear()
        await note_for_agent(_TENANT, self.conversation_id,
                             "The booking form was texted to the customer; the call is ending.")
        return self._end("form_sent", _FORM_SENT)

    async def _phone_turn(self, text: str, current) -> str | None:
        """A reply while a proposal waits on a phone call. None = let the agent handle it."""
        decision = classify_confirmation(text)
        if decision == "approve":
            if not await self._settled():
                return _NOT_SETTLED
            return await self._send_form(current)
        if decision == "decline":
            await self.scheduler.discard(_TENANT, self.conversation_id, current.id)
            self.confirmation.clear()
            self.wrap_up = True
            return "Okay, I've dropped that appointment. Nothing was booked." + _ANYTHING_ELSE
        if decision == "revise":
            await self.scheduler.discard(_TENANT, self.conversation_id, current.id)
            self.confirmation.clear()
            return None
        return self._phone_question(current)

    async def _handle_turn(self, text: str, run_agent) -> str:
        """Coordinate voice confirmation without giving the LLM booking authority.

        Serialized by _agent_lock in _delegate. Uses the scheduler's trusted
        proposal records rather than an LLM-supplied proposal ID.
        """
        tenant = "demo-business"  # Local-only, hardcoded demo identity.
        if await read_choices(self.scheduler, tenant, self.conversation_id, text, emails=False):
            # "Change the time / doctor": the waiting proposal was set aside on purpose.
            self.confirmation.clear()
            self.email_form.clear()
        if is_goodbye(text):
            return await self._goodbye_turn()
        self._leave_warned = False
        current = await self.scheduler.latest_pending(tenant, self.conversation_id)
        armed_id = self.confirmation.proposal_id

        if armed_id and (current is None or current.id != armed_id):
            self.confirmation.clear()
            self.email_form.clear()
            return (
                "That proposal has expired or is no longer available. "
                "I haven't booked anything. Let's choose another time."
            )

        if armed_id and self.channel == "phone":
            reply = await self._phone_turn(text, current)
            if reply is not None:
                return reply
            armed_id = None  # the caller is changing it: the agent prepares a new one

        if armed_id:

            # Voice web calls always require an on-screen typed email. Voice
            # transcripts never verify the email, even if they say "yes".
            if self.email_form.proposal_id != current.id:
                self._arm_email(current)
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

            if self.email_form.verified and (wants_new_email(text) or (
                    "email" in text.casefold() and any(w in text.casefold() for w in
                                                       ("wrong", "change", "incorrect", "correction", "mistake")))):
                self.email_form.clear()
                self._screen_email = None  # ask for the email again
                self._arm_email(current)
                self.needs_readback = False
                return "Sure, I reopened the email box. Please enter the correct address. No invitation has been sent."

            decision = classify_confirmation(text)
            if decision == "approve":
                if not await self._settled():
                    return _NOT_SETTLED
                # IMPORTANT: the ID comes from server state, NOT model output.
                return await self._commit_booking(current)

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
                        "Nothing is booked yet. If the email is incorrect, tap Change "
                        "email on your screen or tell me to change it. Otherwise, tap "
                        "Confirm booking or say 'Confirm booking'."
                    )
                return (
                    "I still have your appointment proposal ready, but I haven't booked "
                    "anything yet. If all details are correct, tap Confirm booking on "
                    "your screen or say 'Confirm booking'. Otherwise, tell me what to change."
                )

        change = await self.scheduler.latest_change(tenant, self.conversation_id)
        if change is not None:
            decision = classify_change_reply(text, change.kind)
            if decision == "approve":
                if not await self._settled():
                    return _NOT_SETTLED
                return await self._commit_change(change)
            if decision == "decline":
                await self.scheduler.discard_change(tenant, self.conversation_id, change.id)
                await note_for_agent(tenant, self.conversation_id,
                                     "The customer chose to keep the appointment unchanged.")
                self.wrap_up = True
                return "No problem, I've kept your appointment exactly as it was." + _ANYTHING_ELSE
            if decision == "unclear":
                booking = await self.scheduler.latest_booking(tenant, self.conversation_id)
                if booking is not None:
                    return self._change_question(change, booking)
            # "revise": let the agent prepare a different change below.

        # Just finished something and asked "anything else?": "no, that's all" ends the call.
        if self.wrap_up and change is None and is_done(text):
            return await self._goodbye_turn()
        self.wrap_up = False

        previous = await self.scheduler.latest_pending(tenant, self.conversation_id)
        previous_change = change
        strikes_before = self.guard.off_topic
        self._end_requested = None
        agent_reply = await run_agent(text)
        if self._end_requested == "done":
            return await self._goodbye_turn()
        if self._end_requested is not None:
            return self._end(self._end_requested)
        off_topic = self._off_topic_reply(strikes_before)
        if off_topic is not None:
            return off_topic
        latest_change = await self.scheduler.latest_change(tenant, self.conversation_id)
        latest = await self.scheduler.latest_pending(tenant, self.conversation_id)
        new_change = latest_change is not None and (
            previous_change is None or latest_change.id != previous_change.id
        )
        new_proposal = latest is not None and (previous is None or latest.id != previous.id)
        if (new_change or new_proposal) and self.guard.count_action():
            # Booking, moving and cancelling over and over: stop before mistakes happen.
            if new_proposal:
                await self.scheduler.discard(tenant, self.conversation_id, latest.id)
            if new_change:
                await self.scheduler.discard_change(tenant, self.conversation_id, latest_change.id)
            return self._end("misuse")
        if new_change:
            booking = await self.scheduler.latest_booking(tenant, self.conversation_id)
            if booking is not None:
                return self._change_question(latest_change, booking)
        if new_proposal:
            self.confirmation.arm(latest.id)
            self.needs_readback = False
            if self.channel == "phone":
                return self._phone_question(latest)
            self._arm_email(latest)
            if self.email_form.verified:
                service = self.scheduler.services.get(latest.service_id)
                details = describe_appointment(latest, service.name if service else latest.service_id).replace(
                    f" Customer email: {latest.customer_email}.", "")
                return (f"I've updated your appointment: {details} I'll use the email you entered earlier. "
                        "Nothing is booked yet. Tap Confirm booking, or say 'Yes, confirm my appointment'.")
            return (
                "I've prepared your appointment. Please enter your email "
                "address in the box on your screen and press Confirm Email. "
                "I won't ask you to spell it. The appointment is not booked yet."
            )
        return agent_reply

    async def _run_agent(self, utterance: str) -> str:
        """One receptionist turn for this call (the agent proposes; ZOVAK's rules decide)."""
        from agents import Runner
        from zovak.agents.context import AgentContext
        from zovak.agents.receptionist import make_receptionist

        memory = memory_session(_TENANT, self.conversation_id)
        status = await booking_status_for_agent(self.scheduler, _TENANT, self.conversation_id, self.channel)
        context = AgentContext(
            tenant_id="demo-business",
            conversation_id=self.conversation_id,
            scheduler=self.scheduler,
            email_via_form=True,
            on_off_topic=self.report_off_topic,
            on_end_call=self.request_end,
            on_misuse=self.report_misuse,
            channel=self.channel,
            customer_phone=self.caller if self.channel == "phone" else None,
        )
        result = await Runner.run(
            make_receptionist(email_via_form=True, booking_status=status, channel=self.channel),
            utterance, context=context, session=memory,
        )
        return str(result.final_output)

    async def pick_slot(self, *, start: str, run_agent=None) -> dict:
        """A time button tapped on the website call: said for the caller, answered aloud.

        Only one of the chosen doctor's free times is accepted. It is handled exactly like
        the caller saying it, so it only proposes; confirming is still a separate step.
        """
        self.guard.caller_active()
        if self.closed:
            raise SchedulingError("The voice session has ended")
        doctor_id = self.scheduler.chosen_doctor(_TENANT, self.conversation_id)
        if doctor_id is None:
            raise SchedulingError("Choose a doctor first")
        try:
            moment = datetime.fromisoformat(start)
        except ValueError as exc:
            raise SchedulingError("That time isn't valid") from exc
        free = await free_days(self.scheduler, _TENANT, self.conversation_id, doctor_id)
        if moment.isoformat() not in {t for d in free for t in d["times"]}:
            raise SchedulingError("That time was just taken. Please pick another one.")
        text = slot_request(moment.astimezone(self.scheduler.timezone), self.scheduler.doctors[doctor_id].name)
        logger.info("Caller tapped a time: %r", text)
        async with self._agent_lock:
            self._turn_heard_at = monotonic()
            reply = await self._handle_turn(text, run_agent or self._run_agent)
        await self._speak(reply)
        return {"message": reply}

    async def _delegate(self, socket, delegation_id: str, offset_ms: int) -> None:
        # A short grace period for transcript fragments; this is NOT a
        # production-grade finished-turn signal.
        await asyncio.sleep(0.6)
        text = self.transcripts.take_for_delegation(offset_ms)
        self._turn_heard_at = monotonic()
        logger.info("Caller said: %r", text[:160])
        if text and not is_meaningful(text):
            reply = self._noise_only_turn()
        elif not text:
            reply = "I didn't catch the full request. Please repeat it."
        else:
            async with self._agent_lock:
                try:
                    reply = await self._handle_turn(text, self._run_agent)
                    logger.info("Nofa replies: %r", reply[:160])
                except Exception:
                    logger.exception("Voice delegated agent or confirmation failed")
                    reply = "Sorry, I couldn't complete that request. Please try again."
        if not self.closed:
            self.guard.agent_speaking(monotonic(), speech_seconds(reply))
            try:
                await socket.send(json.dumps({
                    "type": "session.commentary.append",
                    "event_id": f"zovak_{uuid4().hex}",
                    "delegation_id": delegation_id,
                    "content": reply,
                }))
            except Exception:
                logger.exception("Could not return GPT-Live delegation result")
