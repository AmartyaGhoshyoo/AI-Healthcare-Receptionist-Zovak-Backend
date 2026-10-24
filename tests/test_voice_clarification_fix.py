"""Regression tests: voice clarification preserves server-side booking proposal."""
from datetime import date, timedelta
import pytest
from zovak.services.mock_scheduler import DemoScheduler
from zovak.voice.confirmation import classify_confirmation
from zovak.voice.sideband import VoiceSideband


def test_clarification_does_not_auto_confirm_or_discard():
    assert classify_confirmation(
        "Yes, confirm my appointment, but make sure the email is presented correctly"
    ) == 'unclear'
    assert classify_confirmation('Yes, confirm my appointment, but change the time') == 'revise'
    assert classify_confirmation('Confirm booking') == 'approve'
    assert classify_confirmation('Yes, confirm my') != 'approve'


@pytest.mark.asyncio
async def test_clarification_preserves_proposal_then_explicitly_confirms():
    scheduler = DemoScheduler()
    voice = VoiceSideband(session_id='test-session', conversation_id='voice-test',
                          api_key='unused', scheduler=scheduler, agent_model='unused')
    day = date.today() + timedelta(days=3)
    start = (await scheduler.available_slots('demo-business', 'consultation', day))[0].start

    async def create_proposal(_):
        await scheduler.propose('demo-business', 'voice-test', 'consultation',
                                start, 'Test Customer', 'customer40@gmail.com')
        return 'Pending'

    async def never_call_llm(_):
        raise AssertionError('The confirmation must be deterministic, not an LLM call')

    await voice._handle_turn('Book an appointment', create_proposal)
    proposal = await scheduler.latest_pending('demo-business', 'voice-test')
    assert proposal is not None
    reply = await voice._handle_turn(
        'Yes, confirm my appointment, but make sure the email is presented correctly',
        never_call_llm,
    )
    assert 'box on your screen' in reply
    assert proposal.id not in scheduler._bookings
    assert voice.confirmation.proposal_id == proposal.id
    assert (await scheduler.latest_pending('demo-business', 'voice-test')).id == proposal.id

    await voice.submit_email(request_id=voice.email_form.request_id,email='customer40@gmail.com')
    verified = await voice._handle_turn('I entered the email', never_call_llm)
    assert 'appointment details' in verified
    result = await voice._handle_turn('Confirm booking', never_call_llm)
    assert 'now confirmed' in result
    assert proposal.id in scheduler._bookings


@pytest.mark.asyncio
async def test_split_voice_phrase_not_accidental_confirmation():
    scheduler = DemoScheduler()
    voice = VoiceSideband(session_id='test-session', conversation_id='split-test',
                          api_key='unused', scheduler=scheduler, agent_model='unused')
    day = date.today() + timedelta(days=3)
    start = (await scheduler.available_slots('demo-business', 'consultation', day))[0].start
    async def create(_):
        await scheduler.propose('demo-business', 'split-test', 'consultation', start, 'Test Customer')
        return 'Pending'
    async def never(_):
        raise AssertionError('No LLM call expected')
    await voice._handle_turn('Book', create)
    pending = await scheduler.latest_pending('demo-business', 'split-test')
    await voice._handle_turn('Yes, confirm my', never)
    await voice._handle_turn('appointment', never)
    assert pending.id not in scheduler._bookings
    assert voice.confirmation.proposal_id == pending.id
    result = await voice._handle_turn('Please confirm it', never)
    assert 'box on your screen' in result
    assert pending.id not in scheduler._bookings
