"""Staff portal API (local only; the website's /staff page calls it through Next.js).

Demo login with name + PIN gives a session token sent as X-Staff-Token. Doctors
see and act on their own schedule; reception and the manager see everyone.
"""
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from zovak.services.clinic import STAFF, doctor_card
from zovak.services.mock_scheduler import SchedulingError

router = APIRouter(prefix="/api/v1/staff", tags=["staff portal"])


class Login(BaseModel):
    staff_id: str
    pin: str = Field(min_length=1, max_length=12)


class DelayRequest(BaseModel):
    doctor_id: str | None = None
    minutes: int = Field(ge=5, le=240)
    # Optional "late from" time, e.g. "2026-10-30T13:00" (Dhaka time if no offset).
    late_from: datetime | None = None


class StaffMessage(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


def _staff(request: Request, token: str | None):
    member = request.app.state.staff_sessions.get(token)
    if member is None:
        raise HTTPException(401, detail="Please sign in again")
    return member


def _visible_doctors(request: Request, member) -> list[str]:
    doctors = request.app.state.scheduler.doctors
    return [member.doctor_id] if member.role == "doctor" else list(doctors)


def _me(member) -> dict:
    return {"id": member.id, "name": member.name, "role": member.role, "doctor_id": member.doctor_id}


@router.get("/directory")
async def directory() -> list[dict]:
    """Names for the sign-in screen (never PINs)."""
    return [{"id": m.id, "name": m.name, "role": m.role} for m in STAFF.values()]


@router.post("/login")
async def login(body: Login, request: Request) -> dict:
    result = request.app.state.staff_sessions.login(body.staff_id, body.pin)
    if result is None:
        raise HTTPException(401, detail="Wrong name or PIN (or too many tries; wait 5 minutes)")
    token, member = result
    return {"token": token, "staff": _me(member)}


@router.post("/logout")
async def logout(request: Request, x_staff_token: str | None = Header(default=None)) -> dict:
    request.app.state.staff_sessions.logout(x_staff_token or "")
    return {"ok": True}


@router.get("/me")
async def me(request: Request, x_staff_token: str | None = Header(default=None)) -> dict:
    return _me(_staff(request, x_staff_token))


@router.get("/schedule")
async def schedule(request: Request, day: str | None = None,
                   x_staff_token: str | None = Header(default=None)) -> dict:
    member = _staff(request, x_staff_token)
    scheduler, planner = request.app.state.scheduler, request.app.state.recovery
    tz = scheduler.timezone
    day_date = date.fromisoformat(day) if day else datetime.now(tz).date()
    plan_status = {}
    for plan in planner.plans.values():
        if plan.status == "sent":
            for item in planner.view(plan)["items"]:
                plan_status[item["booking_id"]] = {"status": item["status"], "choice": item["choice"],
                                                   "original_start": item["original_start"]}
    doctors = []
    for doctor_id in _visible_doctors(request, member):
        bookings = sorted(
            (b for b in scheduler._bookings.values()
             if b.doctor_id == doctor_id and b.status == "confirmed"
             and b.start.astimezone(tz).date() == day_date),
            key=lambda b: b.start,
        )
        doctors.append({**doctor_card(scheduler.doctors[doctor_id]), "bookings": [{
            "id": b.id, "start": b.start.isoformat(), "end": b.end.isoformat(),
            "patient": b.customer_name, "phone": b.customer_phone, "email": b.customer_email,
            "service": scheduler.services[b.service_id].name, "source": b.source,
            "delay": plan_status.get(b.id),
        } for b in bookings]})
    return {"day": day_date.isoformat(), "doctors": doctors}


@router.post("/delay/preview")
async def delay_preview(body: DelayRequest, request: Request,
                        x_staff_token: str | None = Header(default=None)) -> dict:
    member = _staff(request, x_staff_token)
    doctor_id = member.doctor_id if member.role == "doctor" else body.doctor_id
    if doctor_id not in _visible_doctors(request, member):
        raise HTTPException(403, detail="You can only report delays for your own schedule")
    try:
        plan = await request.app.state.recovery.preview(doctor_id, body.minutes, member.name,
                                                        late_from=body.late_from)
    except SchedulingError as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    return request.app.state.recovery.view(plan)


@router.post("/delay/{plan_id}/send")
async def delay_send(plan_id: str, request: Request,
                     x_staff_token: str | None = Header(default=None)) -> dict:
    member = _staff(request, x_staff_token)
    planner = request.app.state.recovery
    plan = planner.plans.get(plan_id)
    if plan is None or plan.doctor_id not in _visible_doctors(request, member):
        raise HTTPException(404, detail="Plan not found")
    try:
        return planner.view(await planner.send(plan_id))
    except SchedulingError as exc:
        raise HTTPException(409, detail=str(exc)) from exc


@router.delete("/delay/{plan_id}")
async def delay_discard(plan_id: str, request: Request,
                        x_staff_token: str | None = Header(default=None)) -> dict:
    member = _staff(request, x_staff_token)
    plan = request.app.state.recovery.plans.get(plan_id)
    if plan is None or plan.doctor_id not in _visible_doctors(request, member) or plan.status != "draft":
        raise HTTPException(404, detail="Plan not found")
    plan.status = "discarded"
    return {"ok": True}


@router.get("/plans")
async def plans(request: Request, x_staff_token: str | None = Header(default=None)) -> list[dict]:
    member = _staff(request, x_staff_token)
    planner = request.app.state.recovery
    visible = set(_visible_doctors(request, member))
    sent = [p for p in planner.plans.values() if p.status == "sent" and p.doctor_id in visible]
    return [planner.view(p) for p in sorted(sent, key=lambda p: p.created_at, reverse=True)[:20]]


@router.post("/chat")
async def chat(body: StaffMessage, request: Request,
               x_staff_token: str | None = Header(default=None)) -> dict:
    member = _staff(request, x_staff_token)
    if not request.app.state.settings.openai_api_key:
        raise HTTPException(503, detail="Configure OPENAI_API_KEY first")
    from zovak.agents.staff import run_staff_agent
    reply = await run_staff_agent(member, request.app.state.scheduler, request.app.state.recovery, body.message,
                                  chat_id=request.app.state.staff_sessions.chat_id(x_staff_token))
    return {"reply": reply}
