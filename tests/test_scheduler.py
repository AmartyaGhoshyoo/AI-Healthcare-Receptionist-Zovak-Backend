from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import asyncio
import pytest

from zovak.services.mock_scheduler import DemoScheduler, SchedulingError


@pytest.mark.asyncio
async def test_propose_requires_explicit_confirm_and_rejects_conflicts():
    scheduler = DemoScheduler()
    day = date.today() + timedelta(days=3)
    slots = await scheduler.available_slots("demo-business", "consultation", day)
    assert slots
    start = slots[0].start
    proposal = await scheduler.propose("demo-business", "c1", "consultation", start, "Test Client")
    assert proposal.id not in scheduler._bookings
    confirmed = await scheduler.confirm("demo-business", "c1", proposal.id)
    assert confirmed.status == "confirmed"
    assert (await scheduler.confirm("demo-business", "c1", proposal.id)).booking_id == confirmed.booking_id
    with pytest.raises(SchedulingError, match="Slot unavailable"):
        await scheduler.propose("demo-business", "c2", "consultation", start, "Different Client")


@pytest.mark.asyncio
async def test_confirm_rejects_wrong_conversation():
    scheduler = DemoScheduler()
    slots = await scheduler.available_slots("demo-business", "consultation", date.today() + timedelta(days=3))
    proposed = await scheduler.propose("demo-business", "right", "consultation", slots[0].start, "Customer")
    with pytest.raises(SchedulingError, match="not found"):
        await scheduler.confirm("demo-business", "wrong", proposed.id)


@pytest.mark.asyncio
async def test_race_only_one_confirmed():
    scheduler = DemoScheduler()
    slots = await scheduler.available_slots("demo-business", "consultation", date.today() + timedelta(days=3))
    proposals = await asyncio.gather(*[
        scheduler.propose("demo-business", f"c{i}", "consultation", slots[0].start, f"User {i}")
        for i in range(2)
    ])
    results = await asyncio.gather(*[
        scheduler.confirm("demo-business", p.conversation_id, p.id)
        for p in proposals
    ], return_exceptions=True)
    assert len([r for r in results if not isinstance(r, Exception)]) == 1
    assert len([r for r in results if isinstance(r, SchedulingError)]) == 1


@pytest.mark.asyncio
async def test_naive_datetimes_disallowed():
    scheduler = DemoScheduler()
    with pytest.raises(SchedulingError, match="timezone"):
        await scheduler.propose("demo-business", "conv", "meeting", datetime(2027, 1, 1, 9), "Client")
