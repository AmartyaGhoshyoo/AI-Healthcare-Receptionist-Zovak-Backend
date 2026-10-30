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
    # A pending proposal holds its slot: a second caller can't even propose it.
    proposals = await asyncio.gather(*[
        scheduler.propose("demo-business", f"c{i}", "consultation", slots[0].start, f"User {i}")
        for i in range(2)
    ], return_exceptions=True)
    held = [p for p in proposals if not isinstance(p, Exception)]
    assert len(held) == 1 and isinstance(proposals[1], SchedulingError)
    results = await asyncio.gather(*[
        scheduler.confirm("demo-business", held[0].conversation_id, held[0].id) for _ in range(2)
    ], return_exceptions=True)
    assert len(scheduler._bookings) == 1  # a repeated confirm doesn't double-book


@pytest.mark.asyncio
async def test_hold_expires_and_frees_the_slot():
    from datetime import timezone
    scheduler = DemoScheduler()
    day = date.today() + timedelta(days=3)
    slots = await scheduler.available_slots("demo-business", "consultation", day)
    p = await scheduler.propose("demo-business", "a", "consultation", slots[0].start, "A")
    others = await scheduler.available_slots("demo-business", "consultation", day, conversation_id="b")
    assert slots[0].start not in [s.start for s in others]
    p.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    others = await scheduler.available_slots("demo-business", "consultation", day, conversation_id="b")
    assert slots[0].start in [s.start for s in others]


@pytest.mark.asyncio
async def test_naive_datetimes_disallowed():
    scheduler = DemoScheduler()
    with pytest.raises(SchedulingError, match="timezone"):
        await scheduler.propose("demo-business", "conv", "consultation", datetime(2027, 1, 1, 9), "Client")
