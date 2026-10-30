"""Demo clinic: doctors, the services they offer, staff logins, and demo bookings.

DEMO DATA ONLY. Lives in memory and is recreated on every start. Staff PINs are
for local demos, not real authentication.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from zovak.domain.schemas import ServiceInfo

SERVICES = {
    "consultation": ServiceInfo(id="consultation", name="General consultation", duration_minutes=30),
    "follow_up": ServiceInfo(id="follow_up", name="Follow-up visit", duration_minutes=30),
    "child_checkup": ServiceInfo(id="child_checkup", name="Child check-up", duration_minutes=30),
    "vaccination": ServiceInfo(id="vaccination", name="Child vaccination", duration_minutes=30),
    "skin_consultation": ServiceInfo(id="skin_consultation", name="Skin consultation", duration_minutes=30),
}


@dataclass(frozen=True)
class Doctor:
    id: str
    name: str
    specialty: str
    services: tuple[str, ...]
    start: time
    end: time
    lunch: tuple[time, time]
    about: str = ""

    def works(self, start: datetime, end: datetime) -> bool:
        """Inside working hours and not over lunch (times in the clinic's timezone)."""
        a, b = start.time(), end.time()
        if start.date() != end.date() or a < self.start or b > self.end:
            return False
        return not (a < self.lunch[1] and b > self.lunch[0])


DOCTORS: dict[str, Doctor] = {d.id: d for d in [
    Doctor("dr-rahman", "Dr. Ayesha Rahman", "General Practice", ("consultation", "follow_up"),
           time(9), time(17), (time(13), time(14)), "Family doctor for adults: fever, cough, check-ups."),
    Doctor("dr-chowdhury", "Dr. Imran Chowdhury", "General Practice", ("consultation", "follow_up"),
           time(10), time(18), (time(14), time(15)), "General physician, evening appointments."),
    Doctor("dr-sultana", "Dr. Nusrat Sultana", "Pediatrics", ("child_checkup", "vaccination"),
           time(9), time(15), (time(12), time(13)), "Children's doctor: check-ups and vaccinations."),
    Doctor("dr-karim", "Dr. Farhan Karim", "Dermatology", ("skin_consultation",),
           time(11), time(19), (time(15), time(16)), "Skin specialist: rashes, acne, allergies."),
]}


@dataclass(frozen=True)
class StaffMember:
    id: str
    name: str
    role: str  # "doctor", "receptionist" or "manager"
    pin: str  # DEMO ONLY
    doctor_id: str | None = None
    whatsapp: str | None = None  # registered number for staff mode (later phase)

    @property
    def sees_everyone(self) -> bool:
        return self.role in ("receptionist", "manager")


STAFF: dict[str, StaffMember] = {s.id: s for s in [
    StaffMember("staff-rahman", "Dr. Ayesha Rahman", "doctor", "1111", "dr-rahman"),
    StaffMember("staff-chowdhury", "Dr. Imran Chowdhury", "doctor", "2222", "dr-chowdhury"),
    StaffMember("staff-sultana", "Dr. Nusrat Sultana", "doctor", "3333", "dr-sultana"),
    StaffMember("staff-karim", "Dr. Farhan Karim", "doctor", "4444", "dr-karim"),
    StaffMember("staff-reception", "Rina Akter (Reception)", "receptionist", "5555"),
    StaffMember("staff-manager", "Tanvir Hasan (Clinic manager)", "manager", "9999"),
]}


def doctor_card(doctor: Doctor) -> dict:
    return {
        "id": doctor.id, "name": doctor.name, "specialty": doctor.specialty,
        "services": [SERVICES[s].name for s in doctor.services],
        "service_ids": list(doctor.services),
        "hours": f"{doctor.start.strftime('%I:%M %p').lstrip('0')}–{doctor.end.strftime('%I:%M %p').lstrip('0')}",
        "about": doctor.about,
    }


@dataclass(frozen=True)
class _Patient:
    name: str
    phone: str


_DEMO_PATIENTS = [
    _Patient("Ali Hossain", "+8801700000001"), _Patient("Bina Das", "+8801700000002"),
    _Patient("Chandni Akter", "+8801700000003"), _Patient("Dipu Roy", "+8801700000004"),
    _Patient("Eshita Khan", "+8801700000005"), _Patient("Fahim Ahmed", "+8801700000006"),
    _Patient("Gulshan Ara", "+8801700000007"), _Patient("Habib Mia", "+8801700000008"),
]


def _demo_times(doctor: Doctor, now: datetime, stagger: int = 0) -> list[datetime]:
    """Upcoming demo appointments around "now", so a delay always has someone to affect:
    four back-to-back visits from the next half hour, a gap, then two more; plus two
    tomorrow. If not even one visit fits today, the run starts tomorrow instead."""
    tz = now.tzinfo
    nxt = (now + timedelta(minutes=30 - now.minute % 30 + stagger)).replace(second=0, microsecond=0)
    day = nxt.date()
    start = max(nxt, datetime.combine(day, doctor.start, tz))
    if start + timedelta(minutes=30) > datetime.combine(day, doctor.end, tz):
        day = day + timedelta(days=1)
        start = datetime.combine(day, doctor.start, tz)
    offsets = [0, 30, 60, 90, 240, 270]
    wanted = [start + timedelta(minutes=m) for m in offsets]
    tomorrow = day + timedelta(days=1)
    wanted += [datetime.combine(tomorrow, doctor.start, tz) + timedelta(minutes=m) for m in (30, 120)]
    half_hour = timedelta(minutes=30)
    return [t for t in wanted if doctor.works(t, t + half_hour)]


def seed_demo_bookings(scheduler, *, now: datetime | None = None) -> int:
    """Fill the demo doctors' upcoming hours so the staff portal has something to show.

    Bookings are marked source="demo": moving or cancelling them never touches Google.
    """
    from uuid import uuid4

    from zovak.domain.schemas import BookingProposal

    now = (now or datetime.now(timezone.utc)).astimezone(scheduler.timezone)
    count = 0
    # Stagger the two GPs so one is free when the other runs late (the "no wait" option).
    stagger = {"dr-chowdhury": 120}
    for i, doctor in enumerate(DOCTORS.values()):
        for j, start in enumerate(_demo_times(doctor, now, stagger.get(doctor.id, 0))):
            patient = _DEMO_PATIENTS[(i * 3 + j) % len(_DEMO_PATIENTS)]
            service = doctor.services[0]
            booking = BookingProposal(
                id=str(uuid4()), tenant_id="demo-business", conversation_id=f"demo-{uuid4().hex[:8]}",
                service_id=service, start=start,
                end=start + timedelta(minutes=SERVICES[service].duration_minutes),
                customer_name=patient.name, customer_email=f"{patient.name.split()[0].lower()}@example.com",
                customer_phone=patient.phone, doctor_id=doctor.id, source="demo",
                expires_at=datetime.now(timezone.utc), status="confirmed",
                confirmed_at=datetime.now(timezone.utc) - timedelta(days=1),
            )
            scheduler._bookings[booking.id] = booking
            count += 1
    return count


def clinic_catalog() -> str:
    """The doctors and services as plain lines, given to Nofa up front.

    Saves an AI round trip (a list_doctors call) on the first message of every chat.
    """
    lines = []
    for doctor in DOCTORS.values():
        card = doctor_card(doctor)
        services = ", ".join(f"{sid} ({name})" for sid, name in zip(card["service_ids"], card["services"]))
        lines.append(f"{doctor.id}: {doctor.name} — {doctor.specialty}; services: {services}; "
                     f"hours {card['hours']}. {doctor.about}")
    return "\n".join(lines)
