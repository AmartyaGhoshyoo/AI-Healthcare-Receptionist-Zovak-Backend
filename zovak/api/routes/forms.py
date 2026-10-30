"""Public booking form pages (opened from the SMS sent after a phone call).

These and the phone webhook are the only routes reachable through the public
tunnel. The unguessable one-time token in the URL is the only access key.
"""
from __future__ import annotations

import html
from datetime import date, datetime
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from zovak.services.booking_forms import BookingForm
from zovak.services.mock_scheduler import SchedulingError

router = APIRouter(tags=["booking form (public)"])

_E = html.escape


@router.get("/form/{token}", response_class=HTMLResponse)
async def show_form(token: str, request: Request, day: str | None = None) -> HTMLResponse:
    forms = request.app.state.booking_forms
    form = forms.get(token)
    if form is None:
        return _page("Link not valid", "<p>This booking link is not valid.</p>", status=404)
    if form.status != "open":
        return _done_page(form)
    proposal = forms.proposal(form)
    if proposal is None or proposal.status != "pending":
        return _page("No longer held", "<p>This appointment is no longer held. Please call again to book.</p>")
    tz = forms.scheduler.timezone
    held = proposal.start.astimezone(tz)
    try:
        chosen_day = date.fromisoformat(day) if day else held.date()
    except ValueError:
        chosen_day = held.date()
    try:  # the held time is still listed: holding a slot doesn't block it
        slots = await forms.slots(form, chosen_day)
    except SchedulingError:
        slots = []
    service = forms.scheduler.services.get(form.service_id)
    options = "".join(
        f'<label class="slot"><input type="radio" name="start" value="{_E(s.start.isoformat())}"'
        f'{" checked" if s.start == proposal.start else ""} required>'
        f"<span>{s.start.astimezone(tz).strftime('%I:%M %p').lstrip('0')}</span></label>"
        for s in slots
    ) or '<p class="muted">No free times that day. Pick another day.</p>'
    body = f"""
<p class="muted">Check your details and confirm within the time below. The time is held for you.</p>
<div class="timer" id="timer" data-expires="{int(form.expires_at.timestamp() * 1000)}">2:00</div>
<form method="post" id="booking">
  <label>Name<input name="name" value="{_E(proposal.customer_name)}" maxlength="80" required autocomplete="name"></label>
  <label>Email<input name="email" type="email" inputmode="email" autocapitalize="none" required
    placeholder="you@example.com" autocomplete="email"></label>
  <p class="label">{_E(service.name if service else form.service_id)} with {_E(forms.scheduler.doctor_name(proposal.doctor_id))}<br>{_E(chosen_day.strftime('%A, %B %d'))}</p>
  <div class="slots">{options}</div>
  <p class="small"><a href="?day={(chosen_day.fromordinal(chosen_day.toordinal() + 1)).isoformat()}">Another day →</a>
  {f'<a href="?day={held.date().isoformat()}">Back to {held.strftime("%b %d")}</a>' if chosen_day != held.date() else ''}</p>
  <button name="action" value="confirm" class="primary">Confirm appointment</button>
  <button name="action" value="cancel" class="ghost" formnovalidate>Cancel booking</button>
</form>
<p class="small muted">This form works once. Times are Dhaka time.</p>
<script>
const t = document.getElementById("timer"), end = +t.dataset.expires;
const tick = () => {{
  const s = Math.max(0, Math.round((end - Date.now()) / 1000));
  t.textContent = Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  if (s === 0) {{ t.textContent = "Expired"; document.querySelectorAll("#booking button, #booking input").forEach(e => e.disabled = true); }}
  else setTimeout(tick, 1000);
}};
tick();
</script>"""
    return _page("Confirm your appointment", body)


@router.post("/form/{token}", response_class=HTMLResponse)
async def submit_form(token: str, request: Request) -> HTMLResponse:
    fields = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "replace")).items()}
    forms = request.app.state.booking_forms
    try:
        form = await forms.submit(
            token, action=fields.get("action", ""), name=fields.get("name", ""),
            email=fields.get("email", ""), start_iso=fields.get("start", ""),
        )
    except (SchedulingError, ValueError) as exc:
        back = f'<p><a href="/form/{_E(token)}">← Back to the form</a></p>'
        return _page("Couldn't complete that", f"<p>{_E(str(exc))}.</p>{back}", status=409)
    return _done_page(form)


@router.get("/update/{token}", response_class=HTMLResponse)
async def show_update(token: str, request: Request) -> HTMLResponse:
    """A patient's options after their doctor reported a delay (one-time choice)."""
    planner = request.app.state.recovery
    found = planner.lookup(token)
    if found is None:
        return _page("Link not valid", "<p>This link is not valid.</p>", status=404)
    plan, item = found
    doctor = planner.scheduler.doctor_name(plan.doctor_id)
    if item.choice is not None:
        chosen = next((o.label for o in item.options if o.id == item.choice), "")
        return _page("All set ✓", f"<p>You chose: <b>{_E(chosen)}</b>.</p>"
                     "<p class='muted'>A calendar update is on its way if your time changed.</p>")
    now_at = (f"Your appointment is now at <b>{_E(_local(item.shifted_start, planner))}</b>."
              if item.status == "notified" and item.shifted_start else "Please choose a new time.")
    buttons = "".join(
        f'<button name="option" value="{_E(o.id)}" class="{"ghost" if o.kind == "cancel" else "primary" if o.id == item.default_option else "choice"}">'
        f'{_E(o.label)}{" ✓ current" if o.id == item.default_option and item.status == "notified" else ""}</button>'
        for o in item.options)
    body = (f"<p>{_E(doctor)} is running about <b>{plan.delay_minutes} minutes</b> late. {now_at}</p>"
            f"<p class='muted'>Choose what works best for you. This link works once.</p>"
            f"<form method='post'>{buttons}</form>")
    return _page(f"Hi {item.patient.split()[0]}, your appointment changed", body)


@router.post("/update/{token}", response_class=HTMLResponse)
async def submit_update(token: str, request: Request) -> HTMLResponse:
    fields = {k: v[0] for k, v in parse_qs((await request.body()).decode("utf-8", "replace")).items()}
    try:
        await request.app.state.recovery.choose(token, fields.get("option", ""))
    except SchedulingError as exc:
        return _page("Couldn't do that", f"<p>{_E(str(exc))}.</p><p><a href='/update/{_E(token)}'>← Back</a></p>",
                     status=409)
    return await show_update(token, request)


def _local(moment, planner) -> str:
    return moment.astimezone(planner.scheduler.timezone).strftime("%a %b %d, %I:%M %p")


def _done_page(form: BookingForm) -> HTMLResponse:
    if form.status == "confirmed":
        return _page("You're booked ✓", "<p>Your appointment is confirmed. A calendar invitation is on its way "
                     "to your email.</p><p class='muted'>To change or cancel it later, call us from the same number.</p>")
    if form.status == "cancelled":
        return _page("Cancelled", "<p>Nothing was booked. Call us any time to book again.</p>")
    return _page("This form expired", "<p>The time is no longer held. Please call again to book.</p>")


def _page(title: str, body: str, *, status: int = 200) -> HTMLResponse:
    return HTMLResponse(status_code=status, content=f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_E(title)} · Nofa</title><style>
:root {{ --teal:#0E7490; --ink:#0F2B33; --muted:#5B737C; --line:rgba(15,43,51,.14); --soft:#EAF3F3; --rose:#BE123C; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:#F5F9F9; color:var(--ink); font:16px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif; }}
main {{ max-width:440px; margin:0 auto; padding:24px 16px 40px; }}
.brand {{ font-weight:700; color:var(--teal); letter-spacing:.02em; }}
h1 {{ font-size:1.45rem; margin:6px 0 4px; }}
.card {{ background:#fff; border:1px solid var(--line); border-radius:20px; padding:20px; margin-top:14px;
        box-shadow:0 10px 30px -18px rgba(6,42,51,.35); }}
label {{ display:block; font-weight:600; font-size:.9rem; margin:12px 0 0; }}
input:not([type=radio]) {{ display:block; width:100%; margin-top:6px; padding:12px 14px; font:inherit;
        border:1px solid var(--line); border-radius:12px; }}
input:focus {{ outline:3px solid rgba(14,116,144,.2); border-color:var(--teal); }}
.label {{ font-weight:600; font-size:.9rem; margin:16px 0 8px; }}
.slots {{ display:grid; grid-template-columns:repeat(3,1fr); gap:8px; }}
.slot {{ margin:0; }} .slot input {{ position:absolute; opacity:0; }}
.slot span {{ display:block; text-align:center; padding:10px 4px; border:1px solid var(--line); border-radius:12px;
        font-weight:600; font-size:.9rem; cursor:pointer; }}
.slot input:checked + span {{ background:var(--teal); color:#fff; border-color:var(--teal); }}
.slot input:focus-visible + span {{ outline:3px solid rgba(14,116,144,.3); }}
button {{ display:block; width:100%; margin-top:12px; padding:14px; font:inherit; font-weight:700; border-radius:14px;
        cursor:pointer; border:0; }}
.primary {{ background:var(--teal); color:#fff; }} .ghost {{ background:#fff; color:var(--rose); border:1px solid var(--line); }}
.choice {{ background:var(--soft); color:var(--ink); border:1px solid var(--line); }}
button:disabled {{ opacity:.5; cursor:not-allowed; }}
.timer {{ display:inline-block; background:#FFF1F2; color:var(--rose); font:700 1.1rem ui-monospace,Menlo,monospace;
        padding:6px 14px; border-radius:999px; }}
.muted {{ color:var(--muted); }} .small {{ font-size:.85rem; }} a {{ color:var(--teal); font-weight:600; }}
.small a {{ margin-right:14px; }}
</style></head><body><main><div class="brand">Nofa · Brightside Family Clinic</div>
<h1>{_E(title)}</h1><div class="card">{body}</div></main></body></html>""")
