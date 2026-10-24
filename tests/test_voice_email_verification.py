from datetime import date, timedelta

import pytest

from zovak.voice.email_verification import (
    EmailSpeechError, classify_email_reply, spell_email_for_voice,
)
from zovak.services.mock_scheduler import DemoScheduler
from zovak.voice.sideband import VoiceSideband


def test_spelling_preserves_case_and_symbols():
    got = spell_email_for_voice("Amartya40+VIP@Gmail.com")
    assert got.startswith("uppercase A, lowercase M, lowercase A")
    assert "number four, number zero, plus sign, uppercase V, uppercase I, uppercase P" in got
    assert "at sign, uppercase G" in got
    assert got.endswith("dot, lowercase C, lowercase O, lowercase M")


def test_spelling_example_exactly_speaks_each_character():
    spoken = spell_email_for_voice("amartyaghosh40@gmail.com")
    assert "number four, number zero, at sign" in spoken
    assert "lowercase G, lowercase M, lowercase A, lowercase I, lowercase L" in spoken
    assert len(spoken.split(', ')) == len('amartyaghosh40@gmail.com')


def test_unsupported_address_fails_closed():
    with pytest.raises(EmailSpeechError):
        spell_email_for_voice('customer!tag@example.com')


@pytest.mark.parametrize('utterance', [
    "Yes, the email is correct", "Yes", "that's right", "Email is correct",
])
def test_email_verification_only_on_exact_agreement(utterance):
    assert classify_email_reply(utterance) == 'correct'


@pytest.mark.parametrize('utterance', [
    "Yes, confirm my appointment", "Yes, the email is correct but change the date",
    "Yes, but the email is wrong", "yes the email is correct and confirm my appointment",
    "I think that seems right", "confirm booking",
])
def test_no_combined_or_qualified_consent(utterance):
    assert classify_email_reply(utterance) != 'correct'


@pytest.mark.asyncio
async def test_spoken_email_consent_cannot_replace_visual_input():
    scheduler = DemoScheduler()
    voice = VoiceSideband(session_id='test', conversation_id='legacy-email',
                          api_key='unused', scheduler=scheduler, agent_model='unused')
    start = (await scheduler.available_slots('demo-business', 'consultation',
                date.today()+timedelta(days=5)))[0].start

    async def create(_):
        await scheduler.propose('demo-business', 'legacy-email', 'consultation',
                                start, 'Test Customer', 'Amartya40+VIP@Gmail.com')
        return 'pending'
    async def never(_):
        raise AssertionError('No agent for gate')

    first = await voice._handle_turn('Book', create)
    assert 'box on your screen' in first
    for utterance in ('Yes, the email is correct', 'Yes, confirm my appointment',
                      'stop spelling', 'yes'):
        reply = await voice._handle_turn(utterance, never)
        assert 'box on your screen' in reply
        assert not scheduler._bookings
    request_id = voice.email_form.request_id
    await voice.submit_email(request_id=request_id, email='Amartya40+VIP@Gmail.com')
    readback = await voice._handle_turn('Entered the email', never)
    assert 'appointment details' in readback
    final = await voice._handle_turn('Yes, confirm my appointment', never)
    assert 'now confirmed' in final
    assert scheduler._bookings


@pytest.mark.asyncio
async def test_email_correction_is_through_form_not_spoken_spelling():
    scheduler = DemoScheduler()
    voice = VoiceSideband(session_id='test', conversation_id='change-email',
                          api_key='unused', scheduler=scheduler, agent_model='unused')
    start = (await scheduler.available_slots('demo-business', 'consultation',
                date.today()+timedelta(days=5)))[0].start
    async def create(_):
        await scheduler.propose('demo-business','change-email','consultation',start,
                                'Test Customer')
        return 'pending'
    async def never(_):
        raise AssertionError('Agent should not be called')
    await voice._handle_turn('Book', create)
    await voice.submit_email(request_id=voice.email_form.request_id,email='bad@example.com')
    await voice._handle_turn('Email entered', never)
    spoken = await voice._handle_turn('Change the email please',never)
    assert 'reopened the email box' in spoken
    await voice.submit_email(request_id=voice.email_form.request_id,email='good@example.com')
    latest = await scheduler.latest_pending('demo-business','change-email')
    assert latest.customer_email=='good@example.com'
    assert not scheduler._bookings
