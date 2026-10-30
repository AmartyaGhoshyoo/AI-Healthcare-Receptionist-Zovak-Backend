"""Email each customer a reminder shortly before their appointment.

DEVELOPMENT ONLY: bookings live in process memory, so reminders only go out
while this server is running, and a restart forgets them. Production needs
persisted bookings and reminder jobs.
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timedelta, timezone

import httpx

from zovak.providers.notifications.resend_email import send_resend_email

logger = logging.getLogger(__name__)


def reminder_email(booking, service_name: str, tz) -> tuple[str, str, str]:
    start = booking.start.astimezone(tz)
    when = f"{start.strftime('%A, %B %d')} at {start.strftime('%I:%M %p')} (Dhaka time)"
    subject = f"Reminder: your {service_name} today at {start.strftime('%I:%M %p')}"
    name = html.escape(booking.customer_name)
    body = html.escape(f"{service_name}, {when}")
    page = (
        f"<p>Hi {name},</p>"
        f"<p>This is a reminder of your appointment in about an hour:</p>"
        f"<p><strong>{body}</strong></p>"
        f"<p>Booking reference: {html.escape(booking.id)}</p>"
    )
    text = (
        f"Hi {booking.customer_name},\n\nThis is a reminder of your appointment in about "
        f"an hour:\n{service_name}, {when}\n\nBooking reference: {booking.id}\n"
    )
    return subject, page, text


class ReminderWorker:
    def __init__(self, scheduler, *, api_key: str, sender: str, minutes_before: int = 60,
                 client: httpx.AsyncClient | None = None):
        self.scheduler = scheduler
        self.api_key = api_key
        self.sender = sender
        self.lead = timedelta(minutes=minutes_before)
        self.client = client

    async def run_once(self, now: datetime | None = None) -> list[str]:
        """Send reminders that are due; returns the booking IDs reminded."""
        now = now or datetime.now(timezone.utc)
        sent = []
        for booking in list(self.scheduler._bookings.values()):
            if booking.status != "confirmed" or booking.reminder_sent or not booking.customer_email:
                continue
            if not now < booking.start <= now + self.lead:
                continue
            # Booked (or moved) inside the reminder window: they just got a
            # confirmation, so skip rather than send a near-duplicate.
            if booking.confirmed_at and booking.confirmed_at > booking.start - self.lead:
                booking.reminder_sent = True
                continue
            service = self.scheduler.services.get(booking.service_id)
            subject, page, text = reminder_email(
                booking, service.name if service else booking.service_id, self.scheduler.timezone,
            )
            try:
                await send_resend_email(
                    self.api_key, self.sender, booking.customer_email, subject, page, text,
                    idempotency_key=f"reminder-{booking.id}-{booking.start.isoformat()}",
                    client=self.client,
                )
            except (httpx.HTTPError, RuntimeError):
                logger.warning("Reminder for booking %s failed; will retry", booking.id, exc_info=True)
                continue
            booking.reminder_sent = True
            sent.append(booking.id)
            logger.info("Sent appointment reminder for booking %s", booking.id)
        return sent

    async def run_forever(self, interval_seconds: float = 60) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("Reminder check failed")
            await asyncio.sleep(interval_seconds)
