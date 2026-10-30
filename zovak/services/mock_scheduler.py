"""DEVELOPMENT ONLY. No durable state, external calendar, auth or cross-process locks.

Implements a clear, safe prototype lifecycle:
agent proposes -> user confirms through explicit API -> server checks conflicts -> commit.
"""
import asyncio
from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from zovak.domain.schemas import (
    BookingProposal, ChangeReply, ChangeRequest, ConfirmReply, ServiceInfo, Slot,
)
from zovak.services.clinic import DOCTORS, SERVICES, Doctor


class SchedulingError(Exception):
    pass


MAX_TIME_CHANGES = 1  # a patient may change a confirmed booking's time once


class DemoScheduler:
    provider = "demo-only"

    def __init__(self):
        self.timezone = ZoneInfo("Asia/Dhaka")
        self.services: dict[str, ServiceInfo] = dict(SERVICES)
        self.doctors: dict[str, Doctor] = dict(DOCTORS)
        # Doctors Nofa last suggested in each conversation (shown as cards on the website).
        self._offers: dict[tuple[str, str], list[str]] = {}
        # The doctor the patient picked, until the booking is prepared (a "chosen" chip).
        self._chosen: dict[tuple[str, str], str] = {}
        # The time the patient tapped for that doctor, until the booking is prepared.
        self._picked: dict[tuple[str, str], datetime] = {}
        # The patient's name and typed email in each conversation: a new time (or a second
        # appointment) reuses them, so nobody is asked twice. Kept even if a proposal expires.
        self._contact: dict[tuple[str, str], tuple[str, str | None]] = {}
        # Conversations choosing a new time for their confirmed booking (the one change).
        self._moving: dict[tuple[str, str], str] = {}
        self._proposals: dict[str, BookingProposal] = {}
        self._bookings: dict[str, BookingProposal] = {}
        self._changes: dict[str, ChangeRequest] = {}
        # Most recent cancelled booking per conversation, so the UI can show it.
        self._cancelled: dict[tuple[str, str], BookingProposal] = {}
        self._lock = asyncio.Lock()

    async def list_services(self, tenant_id: str) -> list[ServiceInfo]:
        self._check_tenant(tenant_id)
        return list(self.services.values())

    @staticmethod
    def _check_tenant(tenant_id: str) -> None:
        if tenant_id != "demo-business":
            raise SchedulingError("Unknown demo tenant")

    @staticmethod
    def _same_doctor(a: str | None, b: str | None) -> bool:
        return a is None or b is None or a == b  # no doctor = clinic-wide (older data)

    def _overlaps(self, tenant_id: str, start: datetime, end: datetime,
                  ignore_booking_id: str | None = None, doctor_id: str | None = None) -> bool:
        """Does this doctor already have a booking at that time?"""
        return any(
            booked.tenant_id == tenant_id and booked.id != ignore_booking_id
            and self._same_doctor(booked.doctor_id, doctor_id)
            and start < booked.end and end > booked.start
            for booked in self._bookings.values()
        )

    def _held_by_other(self, tenant_id: str, start: datetime, end: datetime,
                       conversation_id: str | None, doctor_id: str | None = None) -> bool:
        """A pending proposal in ANOTHER conversation holds its doctor's slot until it expires."""
        if conversation_id is None:
            return False
        now = datetime.now(timezone.utc)
        return any(
            p.tenant_id == tenant_id and p.conversation_id != conversation_id
            and p.status == "pending" and p.expires_at >= now
            and self._same_doctor(p.doctor_id, doctor_id)
            and start < p.end and end > p.start
            for p in self._proposals.values()
        )

    def doctors_for(self, service_id: str, doctor_id: str | None = None) -> list[Doctor]:
        if service_id not in self.services:
            raise SchedulingError("Unknown service")
        qualified = [d for d in self.doctors.values() if service_id in d.services]
        if doctor_id is not None:
            qualified = [d for d in qualified if d.id == doctor_id]
            if not qualified:
                raise SchedulingError("That doctor doesn't offer this service")
        return qualified

    def doctor_name(self, doctor_id: str | None) -> str:
        doctor = self.doctors.get(doctor_id or "")
        return doctor.name if doctor else "the clinic"

    async def available_slots(self, tenant_id: str, service_id: str, day: date, *,
                              doctor_id: str | None = None,
                              ignore_booking_id: str | None = None,
                              conversation_id: str | None = None) -> list[Slot]:
        """Free 30-minute starts for a service, per doctor.

        doctor_id limits it to one doctor; otherwise each time is offered once with the
        first free qualified doctor. ignore_booking_id treats that booking's own time as
        free (moving it). conversation_id hides slots held by other conversations.
        """
        self._check_tenant(tenant_id)
        doctors = self.doctors_for(service_id, doctor_id)
        if day < datetime.now(self.timezone).date() or not doctors:
            return []
        duration = timedelta(minutes=self.services[service_id].duration_minutes)
        first = min(d.start for d in doctors)
        last = max(d.end for d in doctors)
        slots: list[Slot] = []
        now = datetime.now(timezone.utc)
        for minute in range(first.hour * 60 + first.minute, last.hour * 60 + last.minute, 30):
            local_start = datetime.combine(day, time(hour=minute // 60, minute=minute % 60), self.timezone)
            local_end = local_start + duration
            if local_start.astimezone(timezone.utc) < now:
                continue
            for doctor in doctors:
                if (doctor.works(local_start, local_end)
                        and not self._overlaps(tenant_id, local_start, local_end,
                                               ignore_booking_id, doctor.id)
                        and not self._held_by_other(tenant_id, local_start, local_end,
                                                    conversation_id, doctor.id)):
                    slots.append(Slot(start=local_start, end=local_end, doctor_id=doctor.id))
                    break
        return slots

    def offer_doctors(self, tenant_id: str, conversation_id: str, doctor_ids: list[str]) -> list[str]:
        """Remember which doctors Nofa suggested, so the website can show them as cards."""
        known = [d for d in doctor_ids if d in self.doctors]
        if len({self.doctors[d].specialty for d in known}) > 1:
            # Never every doctor at once: only those matching the patient's need.
            raise SchedulingError("Offer doctors of one specialty only")
        if self._chosen.get((tenant_id, conversation_id)) in known:
            return known  # that doctor is already chosen: keep the choice, no cards again
        self._offers[(tenant_id, conversation_id)] = known
        self._chosen.pop((tenant_id, conversation_id), None)  # choosing again
        self._picked.pop((tenant_id, conversation_id), None)
        return known

    def offered_doctors(self, tenant_id: str, conversation_id: str) -> list[str]:
        return self._offers.get((tenant_id, conversation_id), [])

    def choose_doctor(self, tenant_id: str, conversation_id: str, doctor_id: str) -> None:
        """The patient picked a doctor: the cards are replaced by a "chosen" chip."""
        if doctor_id in self.doctors:
            self._offers.pop((tenant_id, conversation_id), None)
            if self._chosen.get((tenant_id, conversation_id)) != doctor_id:
                self._picked.pop((tenant_id, conversation_id), None)  # a time was for the old doctor
            self._chosen[(tenant_id, conversation_id)] = doctor_id

    def chosen_doctor(self, tenant_id: str, conversation_id: str) -> str | None:
        return self._chosen.get((tenant_id, conversation_id))

    def _booked_done(self, tenant_id: str, conversation_id: str) -> None:
        """A booking was confirmed: the doctor and time picks were for it, so they're done."""
        key = (tenant_id, conversation_id)
        for picks in (self._offers, self._chosen, self._picked, self._moving):
            picks.pop(key, None)

    async def start_time_change(self, tenant_id: str, conversation_id: str) -> str:
        """The patient wants another time for their confirmed booking: show its doctor's times.

        Returns "ok", "used" (already changed once) or "none" (no booking here).
        """
        booking = await self.latest_booking(tenant_id, conversation_id)
        if booking is None:
            return "none"
        if booking.time_changes >= MAX_TIME_CHANGES:
            return "used"
        key = (tenant_id, conversation_id)
        self._moving[key] = booking.id
        self._chosen[key] = booking.doctor_id
        self._picked.pop(key, None)
        return "ok"

    def moving_booking(self, tenant_id: str, conversation_id: str) -> str | None:
        return self._moving.get((tenant_id, conversation_id))

    def drop_choices(self, tenant_id: str, conversation_id: str) -> None:
        """"Never mind": no doctor cards, time buttons or time change in progress."""
        key = (tenant_id, conversation_id)
        for picks in (self._offers, self._chosen, self._picked, self._moving):
            picks.pop(key, None)

    def has_booking(self, tenant_id: str, conversation_id: str) -> bool:
        return any(b.tenant_id == tenant_id and b.conversation_id == conversation_id
                   and b.status == "confirmed" for b in self._bookings.values())

    def pick_time(self, tenant_id: str, conversation_id: str, start: datetime) -> None:
        """The patient tapped one of the chosen doctor's free times: the card collapses to it."""
        self._picked[(tenant_id, conversation_id)] = start

    def picked_time(self, tenant_id: str, conversation_id: str) -> datetime | None:
        return self._picked.get((tenant_id, conversation_id))

    def unpick_time(self, tenant_id: str, conversation_id: str) -> None:
        """The patient wants a different time: the time buttons come back."""
        self._picked.pop((tenant_id, conversation_id), None)

    async def reopen_times(self, tenant_id: str, conversation_id: str) -> bool:
        """The patient wants another time while a proposal waits: set it aside, show the buttons.

        The doctor stays chosen and the name and email are kept for the next proposal.
        """
        pending = await self.latest_pending(tenant_id, conversation_id)
        if pending is None:
            return False
        key = (tenant_id, conversation_id)
        self._remember_contact(key, pending.customer_name, pending.customer_email)
        await self.discard(tenant_id, conversation_id, pending.id)
        if pending.doctor_id:
            self._chosen[key] = pending.doctor_id
        self._picked.pop(key, None)
        return True

    def carried_details(self, tenant_id: str, conversation_id: str) -> tuple[str, str | None] | None:
        """Name and email the patient already gave in this conversation, if any."""
        return self._contact.get((tenant_id, conversation_id))

    def _remember_contact(self, key: tuple[str, str], name: str | None, email: str | None) -> None:
        known_name, known_email = self._contact.get(key, (None, None))
        self._contact[key] = (name or known_name or "", email or known_email)

    def forget_doctor(self, tenant_id: str, conversation_id: str) -> None:
        """The patient wants a different doctor: start the doctor choice again."""
        self._chosen.pop((tenant_id, conversation_id), None)
        self._picked.pop((tenant_id, conversation_id), None)

    async def propose(
        self, tenant_id: str, conversation_id: str, service_id: str,
        start: datetime, customer_name: str, customer_email: str | None = None, *,
        doctor_id: str | None = None, customer_phone: str | None = None,
    ) -> BookingProposal:
        self._check_tenant(tenant_id)
        if service_id not in self.services:
            raise SchedulingError("Unknown service")
        if start.tzinfo is None:
            raise SchedulingError("An explicit timezone offset is required")
        local_start = start.astimezone(self.timezone)
        day_slots = await self.available_slots(
            tenant_id, service_id, local_start.date(), doctor_id=doctor_id,
            conversation_id=conversation_id,
        )
        slot = next((s for s in day_slots if s.start == local_start), None)
        if slot is None:
            raise SchedulingError("Slot unavailable or outside the doctor's hours")
        # A new time in the same conversation keeps the email the patient already typed,
        # even if the earlier proposal expired meanwhile.
        key = (tenant_id, conversation_id)
        if customer_email is not None:  # only a real address, never a note like "already entered"
            from zovak.voice.email_form import validate_typed_email
            try:
                customer_email = validate_typed_email(customer_email)
            except (SchedulingError, ValueError):
                customer_email = None
        if customer_email is None:
            customer_email = self._contact.get(key, (None, None))[1]
        self._remember_contact(key, customer_name, customer_email)
        proposed = BookingProposal(
            id=str(uuid4()), tenant_id=tenant_id, conversation_id=conversation_id,
            service_id=service_id, start=local_start,
            end=local_start + timedelta(minutes=self.services[service_id].duration_minutes),
            customer_name=customer_name, customer_email=customer_email,
            customer_phone=customer_phone, doctor_id=slot.doctor_id,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        self._offers.pop((tenant_id, conversation_id), None)
        self._chosen.pop((tenant_id, conversation_id), None)
        self._picked.pop((tenant_id, conversation_id), None)
        # Keep only one pending proposal per conversation, so a later change
        # cannot leave an older proposal accidentally confirmable.
        async with self._lock:
            for prior_id, prior in list(self._proposals.items()):
                if (prior.tenant_id == tenant_id
                    and prior.conversation_id == conversation_id
                    and prior.status == "pending"):
                    del self._proposals[prior_id]
            self._proposals[proposed.id] = proposed
        return proposed

    async def set_proposal_email(
        self, tenant_id: str, conversation_id: str, proposal_id: str, email: str
    ) -> BookingProposal:
        """Atomically attach a user-typed email to this conversation's draft."""
        self._check_tenant(tenant_id)
        from zovak.voice.email_form import validate_typed_email
        validated = validate_typed_email(email)
        async with self._lock:
            proposal = self._proposals.get(proposal_id)
            if (proposal is None or proposal.tenant_id != tenant_id
                    or proposal.conversation_id != conversation_id
                    or proposal.status != "pending"):
                raise SchedulingError("This pending appointment is no longer available")
            if datetime.now(timezone.utc) > proposal.expires_at:
                proposal.status = "expired"
                raise SchedulingError("Appointment proposal expired; please start again")
            proposal.customer_email = validated
            self._remember_contact((tenant_id, conversation_id), proposal.customer_name, validated)
            return proposal

    async def clear_proposal_email(self, tenant_id: str, conversation_id: str,
                                   proposal_id: str) -> None:
        """Remove the email from a pending draft so the customer can enter it again."""
        self._check_tenant(tenant_id)
        async with self._lock:
            proposal = self._proposals.get(proposal_id)
            if (proposal is None or proposal.tenant_id != tenant_id
                    or proposal.conversation_id != conversation_id
                    or proposal.status != "pending"):
                raise SchedulingError("This pending appointment is no longer available")
            proposal.customer_email = None
            name, _ = self._contact.get((tenant_id, conversation_id), (proposal.customer_name, None))
            self._contact[(tenant_id, conversation_id)] = (name, None)

    async def latest_pending(self, tenant_id: str, conversation_id: str) -> BookingProposal | None:
        self._check_tenant(tenant_id)
        async with self._lock:
            for item in reversed(list(self._proposals.values())):
                if (item.tenant_id == tenant_id
                    and item.conversation_id == conversation_id
                    and item.status == "pending"
                    and datetime.now(timezone.utc) <= item.expires_at):
                    return item
        return None

    async def discard(self, tenant_id: str, conversation_id: str, proposal_id: str) -> None:
        """Withdraw an unconfirmed proposal; never cancel a confirmed booking."""
        self._check_tenant(tenant_id)
        async with self._lock:
            proposal = self._proposals.get(proposal_id)
            if (proposal is not None and proposal.tenant_id == tenant_id
                and proposal.conversation_id == conversation_id
                and proposal.status == "pending"):
                del self._proposals[proposal_id]

    async def confirm(self, tenant_id: str, conversation_id: str, proposal_id: str) -> ConfirmReply:
        self._check_tenant(tenant_id)
        async with self._lock:  # local single-process guard; NOT production distributed locking
            proposal = self._proposals.get(proposal_id)
            if proposal is None or proposal.tenant_id != tenant_id or proposal.conversation_id != conversation_id:
                raise SchedulingError("Proposal not found for this conversation")
            if proposal.status == "confirmed":
                return ConfirmReply(booking_id=proposal.id, provider="demo-only", status="confirmed")
            if proposal.status != "pending":
                raise SchedulingError("Proposal is no longer pending")
            if datetime.now(timezone.utc) > proposal.expires_at:
                proposal.status = "expired"
                raise SchedulingError("Proposal expired")
            if self._overlaps(tenant_id, proposal.start, proposal.end, doctor_id=proposal.doctor_id):
                proposal.status = "conflict"
                raise SchedulingError("Slot was taken; request a new time")
            proposal.status = "confirmed"
            proposal.confirmed_at = datetime.now(timezone.utc)
            self._bookings[proposal.id] = proposal
            self._booked_done(tenant_id, conversation_id)
            return ConfirmReply(booking_id=proposal.id, provider="demo-only", status="confirmed")

    # ── Move / cancel a confirmed booking (same conversation only) ────────────
    # Bookings live in memory, so only bookings made in this conversation can
    # be found. Finding a returning customer's booking needs durable storage.

    async def latest_booking(self, tenant_id: str, conversation_id: str) -> BookingProposal | None:
        self._check_tenant(tenant_id)
        async with self._lock:
            for item in reversed(list(self._bookings.values())):
                if (item.tenant_id == tenant_id and item.conversation_id == conversation_id
                        and item.status == "confirmed"):
                    return item
        return None

    def last_cancelled(self, tenant_id: str, conversation_id: str) -> BookingProposal | None:
        return self._cancelled.get((tenant_id, conversation_id))

    async def latest_change(self, tenant_id: str, conversation_id: str) -> ChangeRequest | None:
        self._check_tenant(tenant_id)
        async with self._lock:
            for item in reversed(list(self._changes.values())):
                if (item.tenant_id == tenant_id and item.conversation_id == conversation_id
                        and item.status == "pending"
                        and datetime.now(timezone.utc) <= item.expires_at):
                    return item
        return None

    async def _bookable_for_change(self, tenant_id: str, conversation_id: str) -> BookingProposal:
        booking = await self.latest_booking(tenant_id, conversation_id)
        if booking is None:
            raise SchedulingError("There is no confirmed appointment in this conversation")
        if booking.start <= datetime.now(timezone.utc):
            raise SchedulingError("This appointment has already started")
        return booking

    async def _store_change(self, change: ChangeRequest) -> ChangeRequest:
        async with self._lock:
            # One pending change per conversation, like proposals.
            for prior_id, prior in list(self._changes.items()):
                if (prior.tenant_id == change.tenant_id
                        and prior.conversation_id == change.conversation_id
                        and prior.status == "pending"):
                    del self._changes[prior_id]
            self._changes[change.id] = change
        return change

    async def propose_move(self, tenant_id: str, conversation_id: str,
                           new_start: datetime) -> ChangeRequest:
        """Prepare a move ONLY. Nothing changes until apply_change()."""
        self._check_tenant(tenant_id)
        if new_start.tzinfo is None:
            raise SchedulingError("An explicit timezone offset is required")
        booking = await self._bookable_for_change(tenant_id, conversation_id)
        if booking.time_changes >= MAX_TIME_CHANGES:
            raise SchedulingError("The time of this appointment was already changed once. "
                                  "For another change, please contact the clinic.")
        local_start = new_start.astimezone(self.timezone)
        if local_start == booking.start:
            raise SchedulingError("The appointment is already at that time")
        day_slots = await self.available_slots(
            tenant_id, booking.service_id, local_start.date(), doctor_id=booking.doctor_id,
            ignore_booking_id=booking.id, conversation_id=conversation_id,
        )
        if not any(slot.start == local_start for slot in day_slots):
            raise SchedulingError("That new time is unavailable or outside business hours")
        return await self._store_change(ChangeRequest(
            id=str(uuid4()), tenant_id=tenant_id, conversation_id=conversation_id,
            booking_id=booking.id, kind="move", new_start=local_start,
            new_end=local_start + (booking.end - booking.start),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        ))

    async def propose_cancel(self, tenant_id: str, conversation_id: str) -> ChangeRequest:
        """Prepare a cancellation ONLY. Nothing changes until apply_change()."""
        self._check_tenant(tenant_id)
        booking = await self._bookable_for_change(tenant_id, conversation_id)
        return await self._store_change(ChangeRequest(
            id=str(uuid4()), tenant_id=tenant_id, conversation_id=conversation_id,
            booking_id=booking.id, kind="cancel",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        ))

    async def discard_change(self, tenant_id: str, conversation_id: str, change_id: str) -> None:
        """Withdraw a pending move/cancel; the booking stays exactly as it was."""
        self._check_tenant(tenant_id)
        async with self._lock:
            change = self._changes.get(change_id)
            if (change is not None and change.tenant_id == tenant_id
                    and change.conversation_id == conversation_id and change.status == "pending"):
                del self._changes[change_id]
            # "Keep current time": the time change is over (and wasn't used up).
            for picks in (self._moving, self._chosen, self._picked):
                picks.pop((tenant_id, conversation_id), None)

    async def _calendar_apply(self, change: ChangeRequest, booking: BookingProposal) -> None:
        """Hook for an external calendar. Called under the lock, before local state changes."""

    async def apply_change(self, tenant_id: str, conversation_id: str,
                           change_id: str) -> ChangeReply:
        self._check_tenant(tenant_id)
        async with self._lock:
            change = self._changes.get(change_id)
            if (change is None or change.tenant_id != tenant_id
                    or change.conversation_id != conversation_id):
                raise SchedulingError("Change request not found for this conversation")
            booking = self._bookings.get(change.booking_id)
            done = "moved" if change.kind == "move" else "cancelled"
            if change.status == "applied":
                return ChangeReply(booking_id=change.booking_id, kind=change.kind,
                                   provider=self.provider, status=done)
            if change.status != "pending":
                raise SchedulingError("This change request is no longer pending")
            if datetime.now(timezone.utc) > change.expires_at:
                change.status = "expired"
                raise SchedulingError("This change request expired; please ask again")
            if (booking is None or booking.status != "confirmed"
                    or booking.conversation_id != conversation_id):
                raise SchedulingError("The appointment is no longer active")
            if change.kind == "move" and self._overlaps(
                tenant_id, change.new_start, change.new_end, ignore_booking_id=booking.id,
                doctor_id=booking.doctor_id,
            ):
                change.status = "conflict"
                raise SchedulingError("That new time was just taken; choose another time")
            await self._calendar_apply(change, booking)
            if change.kind == "move":
                booking.start, booking.end = change.new_start, change.new_end
                booking.confirmed_at = datetime.now(timezone.utc)
                booking.reminder_sent = False  # the reminder follows the new time
                booking.time_changes += 1  # patients get one change; staff moves don't count
            else:
                booking.status = "cancelled"
                del self._bookings[booking.id]
                self._cancelled[(tenant_id, conversation_id)] = booking
            for picks in (self._moving, self._chosen, self._picked):  # the change is done
                picks.pop((tenant_id, conversation_id), None)
            change.status = "applied"
            return ChangeReply(booking_id=booking.id, kind=change.kind,
                               provider=self.provider, status=done)

    # ── Staff actions (authorized staff, any conversation) ────────────────────

    async def staff_move(self, tenant_id: str, booking_id: str, new_start: datetime, *,
                         doctor_id: str | None = None) -> BookingProposal:
        """Move a confirmed booking (optionally to another doctor). Updates Google for live bookings."""
        self._check_tenant(tenant_id)
        async with self._lock:
            booking = self._bookings.get(booking_id)
            if booking is None or booking.status != "confirmed":
                raise SchedulingError("Booking not found")
            target = doctor_id or booking.doctor_id
            doctor = self.doctors.get(target or "")
            new_start = new_start.astimezone(self.timezone)
            new_end = new_start + (booking.end - booking.start)
            if doctor is not None:
                if booking.service_id not in doctor.services:
                    raise SchedulingError(f"{doctor.name} doesn't offer this service")
                if not doctor.works(new_start, new_end):
                    raise SchedulingError(f"Outside {doctor.name}'s hours")
            if self._overlaps(tenant_id, new_start, new_end, ignore_booking_id=booking.id,
                              doctor_id=target):
                raise SchedulingError("That time is already booked")
            change = ChangeRequest(
                id=str(uuid4()), tenant_id=tenant_id, conversation_id=booking.conversation_id,
                booking_id=booking.id, kind="move", new_start=new_start, new_end=new_end,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            await self._calendar_apply(change, booking)
            booking.start, booking.end, booking.doctor_id = new_start, new_end, target
            booking.confirmed_at = datetime.now(timezone.utc)
            booking.reminder_sent = False
            return booking

    async def staff_cancel(self, tenant_id: str, booking_id: str) -> BookingProposal:
        self._check_tenant(tenant_id)
        async with self._lock:
            booking = self._bookings.get(booking_id)
            if booking is None or booking.status != "confirmed":
                raise SchedulingError("Booking not found")
            change = ChangeRequest(
                id=str(uuid4()), tenant_id=tenant_id, conversation_id=booking.conversation_id,
                booking_id=booking.id, kind="cancel",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            await self._calendar_apply(change, booking)
            booking.status = "cancelled"
            del self._bookings[booking.id]
            self._cancelled[(tenant_id, booking.conversation_id)] = booking
            return booking
