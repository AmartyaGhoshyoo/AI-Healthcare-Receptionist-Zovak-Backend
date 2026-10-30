"""Doctor running late → re-plan only the appointments the delay actually reaches.

Example: Dr. Rahman is 60 min late at 10:00, with patients at 10:00, 10:30, 11:00 and
14:00. Each shifts to max(own time, end of the previous shifted visit): 11:00, 11:30,
12:00 (12:30 would hit lunch, so anything later skips to 14:00). The 14:00 patient is
untouched because the free time before them absorbs the delay — so only 3 patients
hear from us, not the whole day.

Each affected patient gets options, best first:
  1. the same original time with another free doctor of the same service (no wait);
  2. the shifted time with the same doctor (applied by default when staff send the plan);
  3. the next free times with the same doctor (today, then tomorrow);
  4. cancel.
The delay counts from the right moment: now if the doctor is working, their start time
if their day hasn't begun, their next day's start if today is over, or a time staff give
("30 minutes late for my 1:00").
Staff preview first; "send" applies the default (shifted) times — latest first so the
shifts never collide — and notifies each patient with a one-time link. A patient who
doesn't reply keeps the shifted time.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe
from uuid import uuid4

from zovak.agents.memory import note_for_agent
from zovak.services.mock_scheduler import SchedulingError

logger = logging.getLogger(__name__)

TENANT = "demo-business"
NO_REPLY_MINUTES = 15


@dataclass
class Option:
    id: str
    kind: str  # "other_doctor", "shifted", "next_slot", "cancel"
    label: str
    start: datetime | None = None
    doctor_id: str | None = None


@dataclass
class PlanItem:
    booking_id: str
    conversation_id: str
    patient: str
    phone: str | None
    email: str | None
    service_id: str
    original_start: datetime
    shifted_start: datetime | None
    options: list[Option] = field(default_factory=list)
    default_option: str | None = None
    status: str = "planned"  # planned → notified → kept / rescheduled / cancelled / needs_staff
    choice: str | None = None
    notified_via: list[str] = field(default_factory=list)
    token: str = field(default_factory=lambda: token_urlsafe(18))


@dataclass
class DelayPlan:
    id: str
    doctor_id: str
    delay_minutes: int
    reported_by: str
    created_at: datetime
    items: list[PlanItem]
    status: str = "draft"  # draft → sent / discarded
    sent_at: datetime | None = None
    starts_at: datetime | None = None  # the moment the delay counts from
    note: str = ""  # why nobody is affected, in plain words


def _fit(doctor, start: datetime, duration: timedelta) -> datetime | None:
    """Earliest start ≥ start inside the doctor's day, skipping lunch. None if it's past closing."""
    lunch_start = start.replace(hour=doctor.lunch[0].hour, minute=doctor.lunch[0].minute, second=0, microsecond=0)
    lunch_end = start.replace(hour=doctor.lunch[1].hour, minute=doctor.lunch[1].minute, second=0, microsecond=0)
    if start < lunch_end and start + duration > lunch_start:
        start = lunch_end
    closing = start.replace(hour=doctor.end.hour, minute=doctor.end.minute, second=0, microsecond=0)
    return start if start + duration <= closing else None


class RecoveryPlanner:
    def __init__(self, scheduler, *, sms=None, whatsapp=None, email_sender=None,
                 public_base_url: str = "http://127.0.0.1:8000"):
        self.scheduler = scheduler
        self.sms = sms
        self.whatsapp = whatsapp
        self.email_sender = email_sender  # async (to, subject, html, text) -> None
        self.public_base_url = public_base_url.rstrip("/")
        self.plans: dict[str, DelayPlan] = {}
        self._tokens: dict[str, tuple[str, int]] = {}

    # ── Planning ──────────────────────────────────────────────────────────────

    def delay_start(self, doctor, now: datetime, late_from: datetime | None = None) -> datetime:
        """When the delay begins: a given time, else now while working, else the next start."""
        tz = self.scheduler.timezone
        if late_from is not None:
            late_from = (late_from if late_from.tzinfo else late_from.replace(tzinfo=tz)).astimezone(tz)
            if late_from < now - timedelta(hours=1):
                raise SchedulingError("That time has already passed")
            return late_from
        start = datetime.combine(now.date(), doctor.start, tz)
        end = datetime.combine(now.date(), doctor.end, tz)
        if now < start:
            return start  # before their day: late for the start of it
        if now < end:
            return now
        return start + timedelta(days=1)  # their day is over: the next one

    async def preview(self, doctor_id: str, delay_minutes: int, reported_by: str, *,
                      now: datetime | None = None, late_from: datetime | None = None) -> DelayPlan:
        scheduler = self.scheduler
        doctor = scheduler.doctors.get(doctor_id)
        if doctor is None:
            raise SchedulingError("Unknown doctor")
        if not 5 <= delay_minutes <= 240:
            raise SchedulingError("Delay must be between 5 and 240 minutes")
        now = (now or datetime.now(timezone.utc)).astimezone(scheduler.timezone)
        base = self.delay_start(doctor, now, late_from)
        todays = sorted(
            (b for b in scheduler._bookings.values()
             if b.doctor_id == doctor_id and b.status == "confirmed"
             and b.start.astimezone(scheduler.timezone).date() == base.date() and b.end > base),
            key=lambda b: b.start,
        )
        # Pass 1: ripple the delay forward until a gap absorbs it.
        cursor = base + timedelta(minutes=delay_minutes)
        shifted: list[tuple] = []
        for booking in todays:
            if booking.start >= cursor:
                break  # this one and everything after it still fits: untouched
            duration = booking.end - booking.start
            new_start = _fit(doctor, max(booking.start, cursor).astimezone(scheduler.timezone), duration)
            shifted.append((booking, new_start))
            if new_start is not None:
                cursor = new_start + duration
        reserved = [(s, s + (b.end - b.start)) for b, s in shifted if s is not None]

        # Pass 2: options for each affected patient.
        items = []
        taken: set[tuple[str, datetime]] = set()  # other-doctor slots offered in this plan
        for booking, new_start in shifted:
            options: list[Option] = []
            if booking.start > now:
                for other in scheduler.doctors.values():
                    if (other.id != doctor_id and booking.service_id in other.services
                            and (other.id, booking.start) not in taken
                            and other.works(booking.start.astimezone(scheduler.timezone),
                                            booking.end.astimezone(scheduler.timezone))
                            and not scheduler._overlaps(TENANT, booking.start, booking.end,
                                                        doctor_id=other.id)):
                        taken.add((other.id, booking.start))
                        options.append(Option("other", "other_doctor",
                                              f"{_t(booking.start, scheduler)} with {other.name} (no wait)",
                                              booking.start, other.id))
                        break
            if new_start is not None:
                options.append(Option("shifted", "shifted",
                                      f"{_t(new_start, scheduler)} with {doctor.name} (new time)",
                                      new_start, doctor_id))
            arrives = base + timedelta(minutes=delay_minutes)  # nothing earlier with this doctor
            for slot in await self._next_free(booking, reserved, arrives, exclude=new_start):
                options.append(Option(f"slot{len(options)}", "next_slot",
                                      f"{_day_t(slot.start, now, scheduler)} with {doctor.name}",
                                      slot.start, doctor_id))
            options.append(Option("cancel", "cancel", "Cancel my appointment"))
            default = "shifted" if new_start else next((o.id for o in options if o.kind == "next_slot"), None)
            items.append(PlanItem(
                booking_id=booking.id, conversation_id=booking.conversation_id,
                patient=booking.customer_name, phone=booking.customer_phone,
                email=booking.customer_email, service_id=booking.service_id,
                original_start=booking.start, shifted_start=new_start,
                options=options, default_option=default,
            ))
        plan = DelayPlan(id=uuid4().hex[:10], doctor_id=doctor_id, delay_minutes=delay_minutes,
                         reported_by=reported_by, created_at=datetime.now(timezone.utc), items=items,
                         starts_at=base)
        if not items:
            plan.note = self._why_nobody(doctor, base, delay_minutes, todays, now)
        self.plans[plan.id] = plan
        return plan

    def _why_nobody(self, doctor, base: datetime, minutes: int, bookings, now: datetime) -> str:
        """Plain words for staff: why this delay doesn't reach anyone."""
        s = self.scheduler
        day = _day_name(base, now)
        free_at = base + timedelta(minutes=minutes)
        if not bookings:
            return (f"{doctor.name} has no patients {day} from {_t(base, s)}, "
                    "so nobody needs to be told.")
        first = bookings[0]
        return (f"{doctor.name}'s first patient {day} from {_t(base, s)} is {first.customer_name} at "
                f"{_t(first.start, s)}. A {minutes}-minute delay ends at {_t(free_at, s)}, in time for "
                "them, so nobody needs to be told.")

    async def _next_free(self, booking, reserved, after, *, exclude, limit: int = 2):
        """The doctor's next free times after they arrive (today, then tomorrow)."""
        found = []
        for offset in (0, 1):
            day = after.date() + timedelta(days=offset)
            try:
                slots = await self.scheduler.available_slots(
                    TENANT, booking.service_id, day, doctor_id=booking.doctor_id,
                    ignore_booking_id=booking.id,
                )
            except SchedulingError:
                continue
            for slot in slots:
                clash = any(slot.start < end and slot.end > start for start, end in reserved)
                if not clash and slot.start != exclude and slot.start >= after:
                    found.append(slot)
                    if len(found) == limit:
                        return found
        return found

    # ── Sending ───────────────────────────────────────────────────────────────

    async def send(self, plan_id: str) -> DelayPlan:
        plan = self.plans.get(plan_id)
        if plan is None or plan.status != "draft":
            raise SchedulingError("This plan was already sent or discarded")
        doctor = self.scheduler.doctors[plan.doctor_id]
        # Latest first, so each shift moves into time the next one already freed.
        for item in sorted(plan.items, key=lambda i: i.original_start, reverse=True):
            option = next((o for o in item.options if o.id == item.default_option), None)
            if option is None or option.start is None:
                item.status = "needs_staff"
                continue
            try:
                await self.scheduler.staff_move(TENANT, item.booking_id, option.start,
                                                doctor_id=option.doctor_id)
                item.status = "notified"
            except SchedulingError as exc:
                logger.warning("Could not shift booking %s: %s", item.booking_id, exc)
                item.status = "needs_staff"
        plan.status, plan.sent_at = "sent", datetime.now(timezone.utc)
        for index, item in enumerate(plan.items):
            self._tokens[item.token] = (plan.id, index)
            await self._notify(plan, item, doctor)
        return plan

    async def _notify(self, plan: DelayPlan, item: PlanItem, doctor) -> None:
        link = f"{self.public_base_url}/update/{item.token}"
        now_at = (f"Your {_t(item.original_start, self.scheduler)} appointment is now at "
                  f"{_t(item.shifted_start, self.scheduler)}." if item.shifted_start and item.status == "notified"
                  else "We need to find you a new time.")
        text = (f"Nofa: {doctor.name} is running about {plan.delay_minutes} minutes late. {now_at} "
                f"See other options (another doctor, another time, or cancel): {link}")
        if self.sms is not None and item.phone:
            await self.sms.send(item.phone, text)
            item.notified_via.append("sms")
        if self.whatsapp is not None and item.conversation_id.startswith("wa-"):
            try:
                await self.whatsapp.send_text(item.conversation_id[3:], text)
                item.notified_via.append("whatsapp")
            except Exception:
                logger.warning("WhatsApp delay notice failed", exc_info=True)
        if self.email_sender is not None and item.email:
            try:
                await self.email_sender(item.email, f"{doctor.name} is running late",
                                        f"<p>{text.replace(link, f'<a href={link!r}>{link}</a>')}</p>", text)
                item.notified_via.append("email")
            except Exception:
                logger.warning("Email delay notice failed", exc_info=True)
        await note_for_agent(TENANT, item.conversation_id,
                             f"{doctor.name} is running {plan.delay_minutes} minutes late; the patient "
                             "was sent new options. " + now_at)

    # ── Patient's choice ──────────────────────────────────────────────────────

    def lookup(self, token: str) -> tuple[DelayPlan, PlanItem] | None:
        found = self._tokens.get(token)
        if found is None:
            return None
        plan = self.plans[found[0]]
        return plan, plan.items[found[1]]

    async def choose(self, token: str, option_id: str) -> PlanItem:
        found = self.lookup(token)
        if found is None:
            raise SchedulingError("This link is not valid")
        plan, item = found
        if item.choice is not None:
            raise SchedulingError("You already chose an option")
        option = next((o for o in item.options if o.id == option_id), None)
        if option is None:
            raise SchedulingError("That option is no longer available")
        if option.kind == "cancel":
            await self.scheduler.staff_cancel(TENANT, item.booking_id)
            item.status = "cancelled"
        elif option.id == item.default_option and item.status == "notified":
            item.status = "kept"
        else:
            await self.scheduler.staff_move(TENANT, item.booking_id, option.start, doctor_id=option.doctor_id)
            item.status = "rescheduled"
        item.choice = option.id
        await note_for_agent(TENANT, item.conversation_id,
                             f"After the doctor's delay the patient chose: {option.label}.")
        return item

    # ── For the staff portal ──────────────────────────────────────────────────

    def view(self, plan: DelayPlan) -> dict:
        s = self.scheduler
        waited = plan.sent_at and datetime.now(timezone.utc) - plan.sent_at > timedelta(minutes=NO_REPLY_MINUTES)
        return {
            "id": plan.id, "doctor_id": plan.doctor_id, "doctor_name": s.doctor_name(plan.doctor_id),
            "delay_minutes": plan.delay_minutes, "reported_by": plan.reported_by,
            "status": plan.status, "created_at": plan.created_at.isoformat(),
            "sent_at": plan.sent_at.isoformat() if plan.sent_at else None,
            "starts_at": plan.starts_at.isoformat() if plan.starts_at else None,
            "note": plan.note,
            "items": [{
                "booking_id": i.booking_id, "patient": i.patient, "phone": i.phone,
                "service": s.services[i.service_id].name,
                "original_start": i.original_start.isoformat(),
                "shifted_start": i.shifted_start.isoformat() if i.shifted_start else None,
                "options": [{"id": o.id, "kind": o.kind, "label": o.label} for o in i.options],
                "default_option": i.default_option,
                "status": "no_reply" if (i.status == "notified" and waited) else i.status,
                "choice": next((o.label for o in i.options if o.id == i.choice), None),
                "notified_via": i.notified_via,
            } for i in plan.items],
        }


def _t(moment: datetime | None, scheduler) -> str:
    return moment.astimezone(scheduler.timezone).strftime("%I:%M %p").lstrip("0") if moment else "—"


def _day_name(moment: datetime, now: datetime) -> str:
    local, today = moment.date(), now.date()
    return "today" if local == today else "tomorrow" if local == today + timedelta(days=1) \
        else moment.strftime("on %A, %B ") + str(moment.day)


def _day_t(moment: datetime, now: datetime, scheduler) -> str:
    local = moment.astimezone(scheduler.timezone)
    day = "today" if local.date() == now.date() else "tomorrow" if local.date() == now.date() + timedelta(days=1) \
        else local.strftime("%a %b %d")
    return f"{day} {_t(moment, scheduler)}"

