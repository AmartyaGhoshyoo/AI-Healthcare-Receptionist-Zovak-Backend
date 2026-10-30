"""Nofa for clinic staff: schedules and "I'm running late" by chat.

Separate from the patient receptionist. Staff are logged in; doctors only see and
act on their own schedule. Sending a delay plan notifies patients, so the agent
must show the preview and get an explicit "send" first.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from agents import Agent, RunContextWrapper, function_tool

from zovak.agents.receptionist import today_instructions
from zovak.core.config import get_settings
from zovak.services.mock_scheduler import SchedulingError


@dataclass(frozen=True)
class StaffContext:
    staff: object  # StaffMember
    scheduler: object
    planner: object  # RecoveryPlanner

    def doctor_allowed(self, doctor_id: str | None) -> str:
        """The doctor this staff member may act for (a doctor: only themselves)."""
        if self.staff.role == "doctor":
            return self.staff.doctor_id
        if doctor_id not in self.scheduler.doctors:
            raise SchedulingError("Which doctor? Use one of: " + ", ".join(self.scheduler.doctors))
        return doctor_id


@function_tool
async def schedule(ctx: RunContextWrapper[StaffContext], day: str, doctor_id: str | None = None) -> str:
    """Appointments for a day (YYYY-MM-DD). Doctors always get their own; others may pass doctor_id."""
    c = ctx.context
    try:
        target = c.doctor_allowed(doctor_id) if (doctor_id or c.staff.role == "doctor") else None
    except SchedulingError as exc:
        return str(exc)
    day_date = date.fromisoformat(day)
    rows = sorted(
        (b for b in c.scheduler._bookings.values()
         if b.status == "confirmed" and b.start.astimezone(c.scheduler.timezone).date() == day_date
         and (target is None or b.doctor_id == target)),
        key=lambda b: b.start,
    )
    if not rows:
        return "No appointments."
    return "\n".join(
        f"{b.start.astimezone(c.scheduler.timezone).strftime('%I:%M %p')} {b.customer_name} — "
        f"{c.scheduler.services[b.service_id].name} with {c.scheduler.doctor_name(b.doctor_id)}"
        for b in rows
    )


@function_tool
async def preview_delay(ctx: RunContextWrapper[StaffContext], minutes: int, doctor_id: str | None = None,
                        late_from: str | None = None) -> str:
    """Plan a delay WITHOUT notifying anyone: which patients are affected and their options.

    late_from: optional "YYYY-MM-DD HH:MM" (Dhaka time) when the delay begins, e.g. the
    appointment they'll be late for. Leave it out for "running late" right now (before the
    doctor's day starts it counts from their start time; after it ends, from the next day's).
    """
    c = ctx.context
    try:
        target = c.doctor_allowed(doctor_id)
        start = datetime.fromisoformat(late_from) if late_from else None
        plan = await c.planner.preview(target, minutes, c.staff.name, late_from=start)
    except ValueError:
        return "Can't plan that: late_from must look like 2026-10-30 13:00."
    except SchedulingError as exc:
        return f"Can't plan that: {exc}"
    when = plan.starts_at.astimezone(c.scheduler.timezone).strftime("%A %B %d, %I:%M %p")
    if not plan.items:
        return (f"Nobody is affected (delay counted from {when}). {plan.note} "
                "There is nothing to send: don't offer to apply or send a plan.")
    view = c.planner.view(plan)
    lines = [f"plan_id={plan.id}: {len(plan.items)} patient(s) affected by {minutes} min, "
             f"counted from {when}:"]
    for item in view["items"]:
        lines.append(f"- {item['patient']} {item['original_start'][11:16]} → "
                     f"{(item['shifted_start'] or 'needs a new time')[11:16]}; options: "
                     + "; ".join(o["label"] for o in item["options"]))
    lines.append("Ask the staff member to confirm before calling send_delay_plan.")
    return "\n".join(lines)


@function_tool
async def send_delay_plan(ctx: RunContextWrapper[StaffContext], plan_id: str) -> str:
    """Apply a previewed plan and notify the affected patients. Only after explicit confirmation."""
    c = ctx.context
    plan = c.planner.plans.get(plan_id)
    if plan is None:
        return "Unknown plan."
    try:
        c.doctor_allowed(plan.doctor_id)
        plan = await c.planner.send(plan_id)
    except SchedulingError as exc:
        return f"Not sent: {exc}"
    return f"Sent. {len(plan.items)} patient(s) notified; shifted times applied. Answers appear in the portal."


def make_staff_agent(staff) -> Agent[StaffContext]:
    who = (f"You are speaking with {staff.name}, a doctor; act only on their own schedule."
           if staff.role == "doctor" else
           f"You are speaking with {staff.name} ({staff.role}); they can manage every doctor.")
    return Agent[StaffContext](
        name="Nofa Staff Assistant",
        model=get_settings().openai_agent_model,
        instructions=(
            "You are Nofa's assistant for clinic staff. " + who + " " + today_instructions() +
            "Answer schedule questions with the schedule tool. When someone is running late, "
            "call preview_delay every time, even if you planned a delay earlier in this chat "
            "(schedules change; never reuse an old result). If they name a time (\"30 minutes late for my 1:00\"), pass "
            "late_from with that appointment's date and time; if they say when they'll arrive "
            "(\"I won't be in until 2\"), pass late_from = the first appointment they'll miss and "
            "minutes = the gap to their arrival. For \"running late\" now, leave late_from out. "
            "Then summarize who is affected and their options, and ask for "
            "confirmation. Only call send_delay_plan after the staff member clearly confirms "
            "(e.g. 'yes, send it'). Be brief and answer only what was asked: don't bring up earlier "
            "delay plans unless the staff member asks about them. Patients' names and phone numbers are private: "
            "only share them with this logged-in staff member."
        ),
        tools=[schedule, preview_delay, send_delay_plan],
    )


async def run_staff_agent(staff, scheduler, planner, message: str, *, chat_id: str = "default") -> str:
    from agents import Runner

    from zovak.agents.memory import memory_session
    # One memory per sign-in: a restart or new sign-in starts a clean conversation.
    session = memory_session("demo-business", f"staff-{staff.id}-{chat_id}")
    try:
        result = await Runner.run(make_staff_agent(staff), message,
                                  context=StaffContext(staff, scheduler, planner), session=session)
        return str(result.final_output)
    finally:
        await session.close()

