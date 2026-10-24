from datetime import date, timedelta

import pytest

from zovak.services.mock_scheduler import DemoScheduler
from zovak.voice.confirmation import classify_confirmation
from zovak.voice.sideband import VoiceSideband


@pytest.mark.parametrize("speech", [
    "Yes, confirm my appointment",
    "Yes, please confirm my appointment.",
    "Yeah, please confirm that",
    "Sure, please book it",
    "Please confirm the booking",
    "Go ahead and book it",
])
def test_explicit_approvals(speech):
    assert classify_confirmation(speech) == "approve"


@pytest.mark.parametrize("speech", [
    "yes", "maybe", "yes, but make it 5 PM", "No, not yet",
    "hold on", "Actually change the date", "Yes confirm the appointment but tomorrow",
    "I would like information first",
])
def test_no_premature_approval(speech):
    assert classify_confirmation(speech) != "approve"


async def setup_sideband():
    scheduler = DemoScheduler()
    voice = VoiceSideband(
        session_id="test-session", conversation_id="voice-test",
        api_key="unused", scheduler=scheduler, agent_model="unused",
    )
    day = date.today() + timedelta(days=3)
    slots = await scheduler.available_slots("demo-business", "consultation", day)
    start = slots[0].start

    async def agent_request(_: str) -> str:
        await scheduler.propose("demo-business", "voice-test", "consultation", start, "Test Customer")
        return "The appointment has been proposed."

    return voice, scheduler, agent_request


@pytest.mark.asyncio
async def test_requires_separate_explicit_reply():
    voice, scheduler, agent_request = await setup_sideband()
    msg = await voice._handle_turn("Please book an appointment", agent_request)
    assert "box on your screen" in msg
    proposal = await scheduler.latest_pending("demo-business", "voice-test")
    assert proposal and proposal.id not in scheduler._bookings

    async def never_run_agent(_: str) -> str:
        raise AssertionError("Confirmation should not call an LLM")

    msg = await voice._handle_turn("Yes", never_run_agent)
    assert "box on your screen" in msg
    assert proposal.id not in scheduler._bookings
    msg = await voice._handle_turn("Yes, confirm my appointment", never_run_agent)
    assert "box on your screen" in msg
    await voice.submit_email(request_id=voice.email_form.request_id,email='test@example.com')
    await voice._handle_turn('Email entered',never_run_agent)
    msg = await voice._handle_turn("Yes, confirm my appointment", never_run_agent)
    assert "now confirmed" in msg
    assert proposal.id in scheduler._bookings
    assert voice.confirmation.proposal_id is None


@pytest.mark.asyncio
async def test_decline_removes_proposal():
    voice, scheduler, agent_request = await setup_sideband()
    await voice._handle_turn("I'd like a consultation", agent_request)
    previous = await scheduler.latest_pending("demo-business", "voice-test")
    assert previous
    await voice._handle_turn("No, not yet", agent_request)
    assert previous.id not in scheduler._proposals
    assert previous.id not in scheduler._bookings
    assert voice.confirmation.proposal_id is None


@pytest.mark.asyncio
async def test_change_request_discards_old_proposal():
    voice, scheduler, agent_request = await setup_sideband()
    await voice._handle_turn("Book 9 AM", agent_request)
    old = await scheduler.latest_pending("demo-business", "voice-test")
    assert old
    day = date.today() + timedelta(days=3)
    later_slot = (await scheduler.available_slots("demo-business", "consultation", day))[1]

    async def agent_revise(text: str) -> str:
        assert "change" in text.lower()
        await scheduler.propose(
            "demo-business", "voice-test", "consultation", later_slot.start, "Test Customer"
        )
        return "Created revised proposal"

    msg = await voice._handle_turn("Actually change the time", agent_revise)
    latest = await scheduler.latest_pending("demo-business", "voice-test")
    assert latest and latest.id != old.id
    assert old.id not in scheduler._proposals
    assert "box on your screen" in msg


@pytest.mark.asyncio
async def test_without_armed_proposal_does_not_confirm():
    voice, scheduler, agent_request = await setup_sideband()
    async def no_proposal(text: str) -> str:
        return "Nothing to confirm"
    msg = await voice._handle_turn("Yes, confirm my appointment", no_proposal)
    assert msg == "Nothing to confirm"
    assert not scheduler._bookings


@pytest.mark.asyncio
async def test_superseded_proposal_not_confirmable():
    voice, scheduler, agent_request = await setup_sideband()
    await voice._handle_turn("I want a consultation", agent_request)
    prior = await scheduler.latest_pending("demo-business", "voice-test")
    assert prior
    start = (await scheduler.available_slots(
        "demo-business", "consultation", date.today() + timedelta(days=3)
    ))[2].start
    await scheduler.propose("demo-business", "voice-test", "consultation", start, "Test Customer")
    assert prior.id not in scheduler._proposals
    async def not_called(_):
        raise AssertionError("Should not run agent")
    msg = await voice._handle_turn("Yes, confirm my appointment", not_called)
    assert "no longer available" in msg
    assert not scheduler._bookings
