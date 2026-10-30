"""What the website should show for one conversation's booking, from trusted server state.

Shared by text chat and web voice, so both channels follow the same steps:
email_required -> ready_to_confirm -> booked -> (move_pending | cancel_pending) -> cancelled.
"""
from __future__ import annotations

from zovak.domain.schemas import BookingProposal, ChangeRequest
from zovak.services.mock_scheduler import MAX_TIME_CHANGES


def _appointment(scheduler, item: BookingProposal) -> dict:
    service = scheduler.services.get(item.service_id)
    doctor = scheduler.doctors.get(item.doctor_id or "")
    return {
        "id": item.id,
        "service_id": item.service_id,
        "service_name": service.name if service else item.service_id,
        "doctor_id": item.doctor_id,
        "doctor_name": doctor.name if doctor else None,
        "doctor_specialty": doctor.specialty if doctor else None,
        "start": item.start.isoformat(),
        "end": item.end.isoformat(),
        "customer_name": item.customer_name,
        "customer_email": item.customer_email,
    }


def _change(item: ChangeRequest) -> dict:
    return {
        "id": item.id,
        "kind": item.kind,
        "booking_id": item.booking_id,
        "new_start": item.new_start.isoformat() if item.new_start else None,
        "new_end": item.new_end.isoformat() if item.new_end else None,
    }


async def booking_state(scheduler, tenant_id: str, conversation_id: str, *,
                        proposal: BookingProposal | None, email_ready: bool,
                        email_request_id: str | None) -> dict:
    """proposal/email_ready come from the channel, which owns its own email gate."""
    change = await scheduler.latest_change(tenant_id, conversation_id)
    booking = await scheduler.latest_booking(tenant_id, conversation_id)
    cancelled = scheduler.last_cancelled(tenant_id, conversation_id)

    if proposal is not None:
        stage = "ready_to_confirm" if email_ready else "email_required"
    elif change is not None:
        stage = "move_pending" if change.kind == "move" else "cancel_pending"
    elif booking is not None:
        stage = "booked"
    elif cancelled is not None:
        stage = "cancelled"
    else:
        stage = "idle"

    moving = getattr(scheduler, "moving_booking", lambda *_: None)(tenant_id, conversation_id)
    doctor_options, chosen = [], None
    if stage in ("idle", "booked", "cancelled") and proposal is None and change is None:
        doctor_options = await _doctor_options(scheduler, tenant_id, conversation_id)
        chosen_id = scheduler.chosen_doctor(tenant_id, conversation_id)
        if not doctor_options and chosen_id:
            from zovak.services.clinic import doctor_card
            chosen = doctor_card(scheduler.doctors[chosen_id])
            chosen["slots"] = await free_days(scheduler, tenant_id, conversation_id, chosen_id)
            picked = getattr(scheduler, "picked_time", lambda *_: None)(tenant_id, conversation_id)
            chosen["picked"] = picked.isoformat() if picked else None  # the card collapses to it
            chosen["purpose"] = "move" if moving else "book"  # new booking, or changing the booked time
    return {
        "stage": stage,
        "doctor_options": doctor_options,
        "chosen_doctor": chosen,
        "proposal": _appointment(scheduler, proposal) if proposal else None,
        "booking": ({**_appointment(scheduler, booking),
                     # One time change per booking for patients ("Change time" on the booked card).
                     "can_change_time": booking.time_changes < MAX_TIME_CHANGES} if booking else None),
        "change": _change(change) if change else None,
        "cancelled": _appointment(scheduler, cancelled) if cancelled and stage == "cancelled" else None,
        "email_request_id": email_request_id if stage == "email_required" else None,
    }


def slot_request(start, doctor_name: str) -> str:
    """What a tapped time button says for the patient, e.g. "I'd like Friday, October 30 at 11:30 AM with Dr. …"."""
    return f"I'd like {start:%A, %B} {start.day} at {start.strftime('%I:%M %p').lstrip('0')} with {doctor_name}."


async def free_days(scheduler, tenant_id: str, conversation_id: str, doctor_id: str,
                    *, days: int = 3, look_ahead: int = 10) -> list[dict]:
    """The chosen doctor's next days with free times, for tap-to-pick time buttons.

    Only truly free starts: inside their hours, not lunch, not booked, not past, and
    not held by another patient. Every service is 30 minutes, so the first one stands
    in for all of them.
    """
    from datetime import datetime, timedelta

    doctor = scheduler.doctors[doctor_id]
    today = datetime.now(scheduler.timezone).date()
    found = []
    for offset in range(look_ahead):
        day = today + timedelta(days=offset)
        try:
            slots = await scheduler.available_slots(
                tenant_id, doctor.services[0], day, doctor_id=doctor_id, conversation_id=conversation_id,
            )
        except Exception:  # e.g. Google not connected: no buttons, typing still works
            return found
        if slots:
            found.append({"date": day.isoformat(), "times": [s.start.isoformat() for s in slots]})
            if len(found) == days:
                break
    return found


async def _doctor_options(scheduler, tenant_id: str, conversation_id: str) -> list[dict]:
    """Doctors Nofa just suggested, with their next free time, for the website's cards."""
    from datetime import datetime, timedelta

    from zovak.services.clinic import doctor_card
    options = []
    today = datetime.now(scheduler.timezone).date()
    for doctor_id in scheduler.offered_doctors(tenant_id, conversation_id):
        doctor = scheduler.doctors[doctor_id]
        card = doctor_card(doctor)
        card["next_free"] = None
        for offset in range(3):
            try:
                slots = await scheduler.available_slots(
                    tenant_id, doctor.services[0], today + timedelta(days=offset),
                    doctor_id=doctor_id, conversation_id=conversation_id,
                )
            except Exception:  # e.g. Google not connected: show the card without a time
                break
            if slots:
                card["next_free"] = slots[0].start.isoformat()
                break
        options.append(card)
    return options


def _when(scheduler, start, end) -> str:
    a, b = start.astimezone(scheduler.timezone), end.astimezone(scheduler.timezone)
    return f"{a.strftime('%A, %B %d, %Y, %I:%M %p')} to {b.strftime('%I:%M %p')} (Dhaka time)"


async def booking_status_for_agent(scheduler, tenant_id: str, conversation_id: str,
                                   channel: str = "chat") -> str:
    """Plain-text status the agent gets on every turn, straight from the scheduler."""
    lines = []
    booking = await scheduler.latest_booking(tenant_id, conversation_id)
    proposal = await scheduler.latest_pending(tenant_id, conversation_id)
    change = await scheduler.latest_change(tenant_id, conversation_id)
    if booking is not None:
        service = scheduler.services.get(booking.service_id)
        name = service.name if service else booking.service_id
        lines.append(f"CONFIRMED booking: {name} with {scheduler.doctor_name(booking.doctor_id)} "
                     f"for {booking.customer_name}, {_when(scheduler, booking.start, booking.end)}.")
    if proposal is not None:
        lines.append(f"PENDING proposal, not booked yet: {proposal.service_id} with "
                     f"{scheduler.doctor_name(proposal.doctor_id)} at "
                     f"{_when(scheduler, proposal.start, proposal.end)}; email "
                     f"{'entered' if proposal.customer_email else 'not entered yet'}.")
    if change is not None and change.kind == "move":
        lines.append(f"PENDING move, not applied yet: to "
                     f"{_when(scheduler, change.new_start, change.new_end)}.")
    elif change is not None:
        lines.append("PENDING cancellation, not applied yet.")
    if booking is None and scheduler.last_cancelled(tenant_id, conversation_id) is not None:
        lines.append("The booking made earlier in this conversation was cancelled.")
    # What the website shows right now, so Nofa never points at buttons that aren't there.
    chosen = getattr(scheduler, "chosen_doctor", lambda *_: None)(tenant_id, conversation_id)
    picked = getattr(scheduler, "picked_time", lambda *_: None)(tenant_id, conversation_id)
    carried = getattr(scheduler, "carried_details", lambda *_: None)(tenant_id, conversation_id)
    if proposal is None and carried:
        lines.append(f"The patient's name is already known: {carried[0]}. Don't ask for it again."
                     + (" Their email is saved too: call propose_booking WITHOUT customer_email "
                        "(the booking system adds it) and don't ask for it." if carried[1] else ""))
    moving = getattr(scheduler, "moving_booking", lambda *_: None)(tenant_id, conversation_id)
    if proposal is None and chosen and moving and channel in ("chat", "web"):
        lines.append(f"WEBSITE SCREEN: the patient is choosing a NEW time for their confirmed booking "
                     f"with {scheduler.doctor_name(chosen)} (their one allowed change); the time buttons "
                     "are showing. If they type a time instead of tapping, call propose_move, never "
                     "propose_booking.")
    elif proposal is None and chosen and channel in ("chat", "web"):  # phone/WhatsApp have no screen
        lines.append(f"WEBSITE SCREEN: {scheduler.doctor_name(chosen)} is chosen" + (
            f"; the patient picked {picked.astimezone(scheduler.timezone):%A, %B %d at %I:%M %p} with the time buttons."
            if picked else "; their free times are showing as tap buttons."))
    return " ".join(lines) or "No appointment in this conversation yet."
