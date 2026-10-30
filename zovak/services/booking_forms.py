"""One-time booking forms sent by SMS after a phone call.

On a call, saying "confirm" only sends the form. The booking is made when the
customer submits the form: they can fix their name, add their email, pick
another free time, or cancel. Each form works once and expires quickly; the
slot is held for the same time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from secrets import token_urlsafe
from typing import Literal

from zovak.agents.memory import note_for_agent
from zovak.services.mock_scheduler import SchedulingError
from zovak.voice.email_form import validate_typed_email

FormStatus = Literal["open", "confirmed", "cancelled", "expired"]


@dataclass
class BookingForm:
    token: str
    tenant_id: str
    conversation_id: str
    proposal_id: str
    phone: str
    service_id: str
    expires_at: datetime
    status: FormStatus = "open"
    booking_id: str | None = None


class BookingForms:
    def __init__(self, scheduler, *, minutes: int = 2):
        self.scheduler = scheduler
        self.lifetime = timedelta(minutes=minutes)
        self._forms: dict[str, BookingForm] = {}

    async def create(self, tenant_id: str, proposal, phone: str) -> BookingForm:
        """A form for this pending proposal; the slot is held until the form expires."""
        expires = datetime.now(timezone.utc) + self.lifetime
        proposal.expires_at = expires + timedelta(seconds=15)  # small grace for a late tap
        form = BookingForm(
            token=token_urlsafe(24), tenant_id=tenant_id,
            conversation_id=proposal.conversation_id, proposal_id=proposal.id,
            phone=phone, service_id=proposal.service_id, expires_at=expires,
        )
        self._forms[form.token] = form
        return form

    def get(self, token: str) -> BookingForm | None:
        form = self._forms.get(token)
        if form and form.status == "open" and datetime.now(timezone.utc) > form.expires_at:
            form.status = "expired"
        return form

    def proposal(self, form: BookingForm):
        return self.scheduler._proposals.get(form.proposal_id)

    async def slots(self, form: BookingForm, day: date):
        """Free times with the same doctor (the patient chose them on the call)."""
        proposal = self.proposal(form)
        return await self.scheduler.available_slots(
            form.tenant_id, form.service_id, day, conversation_id=form.conversation_id,
            doctor_id=proposal.doctor_id if proposal else None,
        )

    async def submit(self, token: str, *, action: str, name: str = "", email: str = "",
                     start_iso: str = "") -> BookingForm:
        form = self.get(token)
        if form is None:
            raise SchedulingError("This form link is not valid")
        if form.status != "open":
            raise SchedulingError({
                "confirmed": "This form was already used to confirm the appointment",
                "cancelled": "This form was already used to cancel",
                "expired": "This form expired. Please call again to book",
            }[form.status])
        scheduler, tenant, conv = self.scheduler, form.tenant_id, form.conversation_id

        if action == "cancel":
            await scheduler.discard(tenant, conv, form.proposal_id)
            form.status = "cancelled"
            await note_for_agent(tenant, conv, "The customer cancelled using the SMS form.")
            return form
        if action != "confirm":
            raise SchedulingError("Unknown action")

        name = " ".join(name.split())
        if not 1 <= len(name) <= 80:
            raise ValueError("Please enter your name")
        email = validate_typed_email(email.strip())
        proposal = self.proposal(form)
        if proposal is None or proposal.status != "pending":
            raise SchedulingError("This appointment is no longer held. Please call again to book")
        start = datetime.fromisoformat(start_iso) if start_iso else proposal.start
        if start != proposal.start:
            # A different free time: replace the draft, then confirm that one.
            proposal = await scheduler.propose(tenant, conv, form.service_id, start, name,
                                               customer_email=email, doctor_id=proposal.doctor_id,
                                               customer_phone=proposal.customer_phone)
            form.proposal_id = proposal.id
        else:
            proposal.customer_name = name
            await scheduler.set_proposal_email(tenant, conv, proposal.id, email)
        result = await scheduler.confirm(tenant, conv, proposal.id)
        form.status = "confirmed"
        form.booking_id = result.booking_id
        await note_for_agent(tenant, conv, "The customer confirmed the appointment using the SMS form.")
        return form
