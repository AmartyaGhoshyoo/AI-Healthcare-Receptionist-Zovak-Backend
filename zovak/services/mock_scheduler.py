"""DEVELOPMENT ONLY. No durable state, external calendar, auth or cross-process locks.

Implements a clear, safe prototype lifecycle:
agent proposes -> user confirms through explicit API -> server checks conflicts -> commit.
"""
import asyncio
from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from zovak.domain.schemas import BookingProposal, ConfirmReply, ServiceInfo, Slot


class SchedulingError(Exception):
    pass


class DemoScheduler:
    def __init__(self):
        self.timezone = ZoneInfo("Asia/Dhaka")
        self.services = {
            "consultation": ServiceInfo(id="consultation", name="General consultation", duration_minutes=30),
            "meeting": ServiceInfo(id="meeting", name="Business meeting", duration_minutes=60),
        }
        self._proposals: dict[str, BookingProposal] = {}
        self._bookings: dict[str, BookingProposal] = {}
        self._lock = asyncio.Lock()

    async def list_services(self, tenant_id: str) -> list[ServiceInfo]:
        self._check_tenant(tenant_id)
        return list(self.services.values())

    @staticmethod
    def _check_tenant(tenant_id: str) -> None:
        if tenant_id != "demo-business":
            raise SchedulingError("Unknown demo tenant")

    def _overlaps(self, tenant_id: str, start: datetime, end: datetime) -> bool:
        return any(
            booked.tenant_id == tenant_id and start < booked.end and end > booked.start
            for booked in self._bookings.values()
        )

    async def available_slots(self, tenant_id: str, service_id: str, day: date) -> list[Slot]:
        self._check_tenant(tenant_id)
        if service_id not in self.services:
            raise SchedulingError("Unknown service")
        if day < datetime.now(self.timezone).date():
            return []
        duration = timedelta(minutes=self.services[service_id].duration_minutes)
        slots: list[Slot] = []
        now = datetime.now(timezone.utc)
        for minute in range(9 * 60, 17 * 60, 30):
            local_start = datetime.combine(day, time(hour=minute // 60, minute=minute % 60), self.timezone)
            local_end = local_start + duration
            if local_end.date() != day or local_end.time() > time(17, 0):
                continue
            if local_start.astimezone(timezone.utc) < now:
                continue
            if not self._overlaps(tenant_id, local_start, local_end):
                slots.append(Slot(start=local_start, end=local_end))
        return slots

    async def propose(
        self, tenant_id: str, conversation_id: str, service_id: str,
        start: datetime, customer_name: str, customer_email: str | None = None,
    ) -> BookingProposal:
        self._check_tenant(tenant_id)
        if service_id not in self.services:
            raise SchedulingError("Unknown service")
        if start.tzinfo is None:
            raise SchedulingError("An explicit timezone offset is required")
        local_start = start.astimezone(self.timezone)
        day_slots = await self.available_slots(tenant_id, service_id, local_start.date())
        if not any(slot.start == local_start for slot in day_slots):
            raise SchedulingError("Slot unavailable or outside business hours")
        proposed = BookingProposal(
            id=str(uuid4()), tenant_id=tenant_id, conversation_id=conversation_id,
            service_id=service_id, start=local_start,
            end=local_start + timedelta(minutes=self.services[service_id].duration_minutes),
            customer_name=customer_name, customer_email=customer_email,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
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
            return proposal

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
            if self._overlaps(tenant_id, proposal.start, proposal.end):
                proposal.status = "conflict"
                raise SchedulingError("Slot was taken; request a new time")
            proposal.status = "confirmed"
            self._bookings[proposal.id] = proposal
            return ConfirmReply(booking_id=proposal.id, provider="demo-only", status="confirmed")
