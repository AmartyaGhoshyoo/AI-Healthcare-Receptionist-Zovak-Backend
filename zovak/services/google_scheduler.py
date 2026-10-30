"""Development-only Google-backed scheduler for the existing voice/chat agent.

Single user, single process, volatile OAuth credentials. Not a production-safe
booking transaction; Google Calendar doesn't lock a free slot during insert.
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from zovak.domain.schemas import BookingProposal, ChangeRequest, ConfirmReply, Slot
from zovak.providers.calendar.google_invites import GoogleInvites
from zovak.services.mock_scheduler import DemoScheduler, SchedulingError


def describe_appointment(proposal: BookingProposal, service_name: str) -> str:
    from zovak.services.clinic import DOCTORS
    start = proposal.start.astimezone(ZoneInfo("Asia/Dhaka"))
    end = proposal.end.astimezone(ZoneInfo("Asia/Dhaka"))
    minutes = round((end - start).total_seconds() / 60)
    email = f" Customer email: {proposal.customer_email}." if proposal.customer_email else ""
    doctor = DOCTORS.get(proposal.doctor_id or "")
    with_doctor = f" with {doctor.name}" if doctor else ""
    return (
        f"Customer: {proposal.customer_name}. Service: {service_name}{with_doctor}. "
        f"Date: {start.strftime('%B %d, %Y')}. "
        f"Time: {start.strftime('%I:%M %p')} to {end.strftime('%I:%M %p')}, Dhaka time. "
        f"Duration: {minutes} minutes.{email}"
    )


def google_event_id(booking_id: str) -> str:
    return "a" + booking_id.replace("-", "")  # deterministic base32hex ID


class GoogleBackedScheduler(DemoScheduler):
    provider = "google-calendar"

    def __init__(self, app, *, google_client: httpx.AsyncClient | None = None):
        super().__init__()
        self.app = app
        self.google_client = google_client
        self._token_lock = asyncio.Lock()

    async def _google(self) -> GoogleInvites:
        tokens = getattr(self.app.state, "google_oauth_tokens", None)
        if not tokens:
            raise SchedulingError("Google Calendar not connected. Open /api/v1/auth/google/start")
        if tokens.get("expires_at", 0) < time.time() + 90:
            async with self._token_lock:
                # Another coroutine may have refreshed while we waited.
                tokens = getattr(self.app.state, "google_oauth_tokens", None)
                if not tokens:
                    raise SchedulingError("Google Calendar disconnected")
                if tokens.get("expires_at", 0) < time.time() + 90:
                    refresh_token = tokens.get("refresh_token")
                    if not refresh_token:
                        raise SchedulingError("Google access token expired; reconnect Google Calendar")
                    from zovak.api.routes.google_oauth_dev import OAuthSettings
                    cfg = OAuthSettings()
                    try:
                        async with httpx.AsyncClient(timeout=15) as client:
                            reply = await client.post("https://oauth2.googleapis.com/token", data={
                                "grant_type": "refresh_token", "refresh_token": refresh_token,
                                "client_id": cfg.google_client_id,
                                "client_secret": cfg.google_client_secret,
                            })
                            reply.raise_for_status()
                            payload = reply.json()
                        if not payload.get("access_token"):
                            raise ValueError("Missing access token")
                    except (httpx.HTTPError, ValueError) as exc:
                        raise SchedulingError("Google Calendar authorization expired; reconnect") from exc
                    tokens = {
                        **tokens,
                        "access_token": payload["access_token"],
                        "expires_at": time.time() + int(payload.get("expires_in", 3600)),
                        "refresh_token": payload.get("refresh_token") or refresh_token,
                    }
                    self.app.state.google_oauth_tokens = tokens
        return GoogleInvites(tokens["access_token"], client=self.google_client)

    def _external_busy(self, busy: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
        """Busy time from events ZOVAK didn't create (meetings, holidays…).

        The clinic shares one Google calendar, so our own bookings show up as busy
        for every doctor. Free/busy has no event IDs and merges back-to-back events,
        so subtract our live bookings' intervals; what's left blocks all doctors.
        Each doctor's own bookings are checked locally, per doctor.
        """
        own = sorted((b.start, b.end) for b in self._bookings.values()
                     if b.status == "confirmed" and b.source == "live")
        external = []
        for start, end in busy:
            pieces = [(start, end)]
            for own_start, own_end in own:
                pieces = [piece for a, b in pieces for piece in (
                    [(a, b)] if own_end <= a or own_start >= b
                    else [p for p in ((a, own_start), (own_end, b)) if p[0] < p[1]]
                )]
            external.extend(pieces)
        return external

    async def available_slots(self, tenant_id: str, service_id: str, day: date, *,
                              doctor_id: str | None = None,
                              ignore_booking_id: str | None = None,
                              conversation_id: str | None = None) -> list[Slot]:
        # Doctor hours, each doctor's bookings and other callers' holds still apply.
        local_slots = await super().available_slots(
            tenant_id, service_id, day, doctor_id=doctor_id,
            ignore_booking_id=ignore_booking_id, conversation_id=conversation_id,
        )
        if not local_slots:
            return []
        cal = await self._google()  # fail closed; NEVER silently fall back to demo
        business_day_start = datetime.combine(day, dt_time(0, 0), self.timezone)
        business_day_end = business_day_start + timedelta(days=1)
        try:
            busy = await cal.busy_intervals(business_day_start, business_day_end)
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            raise SchedulingError("Could not verify Google Calendar availability") from exc
        busy = self._external_busy(busy)
        return [slot for slot in local_slots if not any(
            slot.start < busy_end and slot.end > busy_start
            for busy_start, busy_end in busy
        )]

    async def propose(self, tenant_id: str, conversation_id: str, service_id: str,
                      start: datetime, customer_name: str,
                      customer_email: str | None = None, *, doctor_id: str | None = None,
                      customer_phone: str | None = None) -> BookingProposal:
        # A web-voice proposal may be drafted WITHOUT an email: it can't be confirmed until a
        # validated address is typed. The base scheduler validates the email and reuses the one
        # already typed in this conversation, so it must not be overwritten here.
        return await super().propose(tenant_id, conversation_id, service_id, start, customer_name,
                                     customer_email, doctor_id=doctor_id, customer_phone=customer_phone)

    async def confirm(self, tenant_id: str, conversation_id: str, proposal_id: str) -> ConfirmReply:
        self._check_tenant(tenant_id)
        async with self._lock:  # Only local process. No cross-worker or Google atomicity.
            proposal = self._proposals.get(proposal_id)
            if (proposal is None or proposal.tenant_id != tenant_id
                or proposal.conversation_id != conversation_id):
                raise SchedulingError("Proposal not found for this conversation")
            if proposal.status == "confirmed":
                return ConfirmReply(booking_id=proposal.id, provider="google-calendar", status="confirmed")
            if proposal.status != "pending":
                raise SchedulingError("Proposal is no longer pending")
            if datetime.now(timezone.utc) > proposal.expires_at:
                proposal.status = "expired"
                raise SchedulingError("Proposal expired; please start a new request")
            if not proposal.customer_email:
                raise SchedulingError("Customer email address is required")
            if self._overlaps(tenant_id, proposal.start, proposal.end, doctor_id=proposal.doctor_id):
                proposal.status = "conflict"
                raise SchedulingError("Slot was taken; choose a different time")
            cal = await self._google()
            try:
                event_id = google_event_id(proposal.id)
                already_created = await cal.invited_event_exists(
                    event_id=event_id, start=proposal.start, end=proposal.end,
                    customer_email=proposal.customer_email,
                )
                # A previous insertion may have succeeded but the HTTP response
                # was lost. In that case accept verified matching event.
                if not already_created:
                    busy = self._external_busy(await cal.busy_intervals(proposal.start, proposal.end))
                    if busy:
                        proposal.status = "conflict"
                        raise SchedulingError("Google Calendar is busy; choose a different time")
                    service = self.services[proposal.service_id]
                    await cal.create_invited_event(
                        event_id=event_id,
                        title=f"{service.name} - {proposal.customer_name} ({self.doctor_name(proposal.doctor_id)})",
                        start=proposal.start, end=proposal.end,
                        customer_email=proposal.customer_email,
                        description=f"Booked with ZOVAK (development). Booking reference: {proposal.id}",
                    )
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                # Could have created the event before a network timeout. Retain
                # pending state so user can retry, with deterministic event ID.
                raise SchedulingError("Google could not verify event creation; retry or check Calendar") from exc
            proposal.status = "confirmed"
            proposal.confirmed_at = datetime.now(timezone.utc)
            self._bookings[proposal.id] = proposal
            self._booked_done(tenant_id, conversation_id)
            return ConfirmReply(booking_id=proposal.id, provider="google-calendar", status="confirmed")

    async def _calendar_apply(self, change: ChangeRequest, booking: BookingProposal) -> None:
        """Move or delete the booking's Google event before local state changes."""
        if booking.source == "demo":
            return  # demo bookings were never put in Google Calendar
        cal = await self._google()
        event_id = google_event_id(booking.id)
        try:
            if change.kind == "move":
                busy = self._external_busy(await cal.busy_intervals(change.new_start, change.new_end))
                if busy:
                    change.status = "conflict"
                    raise SchedulingError("Google Calendar is busy at the new time; choose another")
                await cal.move_event(event_id=event_id, start=change.new_start, end=change.new_end)
            else:
                await cal.cancel_event(event_id=event_id)
        except (httpx.HTTPError, RuntimeError, ValueError, KeyError) as exc:
            action = "move" if change.kind == "move" else "cancel"
            raise SchedulingError(
                f"Google could not verify the {action}; nothing was changed. Please retry"
            ) from exc
