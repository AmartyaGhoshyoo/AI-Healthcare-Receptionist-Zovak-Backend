"""Development-only Google-backed scheduler for the existing voice/chat agent.

Single user, single process, volatile OAuth credentials. Not a production-safe
booking transaction; Google Calendar doesn't lock a free slot during insert.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from zovak.domain.schemas import BookingProposal, ConfirmReply, Slot
from zovak.providers.calendar.google_invites import GoogleInvites
from zovak.services.mock_scheduler import DemoScheduler, SchedulingError

_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def describe_appointment(proposal: BookingProposal, service_name: str) -> str:
    start = proposal.start.astimezone(ZoneInfo("Asia/Dhaka"))
    end = proposal.end.astimezone(ZoneInfo("Asia/Dhaka"))
    minutes = round((end - start).total_seconds() / 60)
    email = f" Customer email: {proposal.customer_email}." if proposal.customer_email else ""
    return (
        f"Customer: {proposal.customer_name}. Service: {service_name}. "
        f"Date: {start.strftime('%B %d, %Y')}. "
        f"Time: {start.strftime('%I:%M %p')} to {end.strftime('%I:%M %p')}, Dhaka time. "
        f"Duration: {minutes} minutes.{email}"
    )


class GoogleBackedScheduler(DemoScheduler):
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

    async def available_slots(self, tenant_id: str, service_id: str, day: date) -> list[Slot]:
        # Local opening hours and confirmed appointments still apply.
        local_slots = await super().available_slots(tenant_id, service_id, day)
        if not local_slots:
            return []
        cal = await self._google()  # fail closed; NEVER silently fall back to demo
        business_day_start = datetime.combine(day, dt_time(0, 0), self.timezone)
        business_day_end = business_day_start + timedelta(days=1)
        try:
            busy = await cal.busy_intervals(business_day_start, business_day_end)
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            raise SchedulingError("Could not verify Google Calendar availability") from exc
        return [slot for slot in local_slots if not any(
            slot.start < busy_end and slot.end > busy_start
            for busy_start, busy_end in busy
        )]

    async def propose(self, tenant_id: str, conversation_id: str, service_id: str,
                      start: datetime, customer_name: str,
                      customer_email: str | None = None) -> BookingProposal:
        # A web-voice proposal may be drafted WITHOUT an email. It cannot be
        # confirmed until a validated address is submitted via the form.
        if customer_email is not None and (
            not _EMAIL.fullmatch(customer_email) or len(customer_email) > 254
        ):
            raise SchedulingError("Invalid customer email address")
        proposal = await super().propose(tenant_id, conversation_id, service_id, start, customer_name)
        proposal.customer_email = customer_email
        return proposal

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
            if self._overlaps(tenant_id, proposal.start, proposal.end):
                proposal.status = "conflict"
                raise SchedulingError("Slot was taken; choose a different time")
            cal = await self._google()
            try:
                event_id = "a" + proposal.id.replace("-", "")  # deterministic base32hex ID
                already_created = await cal.invited_event_exists(
                    event_id=event_id, start=proposal.start, end=proposal.end,
                    customer_email=proposal.customer_email,
                )
                # A previous insertion may have succeeded but the HTTP response
                # was lost. In that case accept verified matching event.
                if not already_created:
                    busy = await cal.busy_intervals(proposal.start, proposal.end)
                    if busy:
                        proposal.status = "conflict"
                        raise SchedulingError("Google Calendar is busy; choose a different time")
                    service = self.services[proposal.service_id]
                    await cal.create_invited_event(
                        event_id=event_id,
                        title=f"{service.name} - {proposal.customer_name}",
                        start=proposal.start, end=proposal.end,
                        customer_email=proposal.customer_email,
                        description=f"Booked with ZOVAK (development). Booking reference: {proposal.id}",
                    )
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                # Could have created the event before a network timeout. Retain
                # pending state so user can retry, with deterministic event ID.
                raise SchedulingError("Google could not verify event creation; retry or check Calendar") from exc
            proposal.status = "confirmed"
            self._bookings[proposal.id] = proposal
            return ConfirmReply(booking_id=proposal.id, provider="google-calendar", status="confirmed")
