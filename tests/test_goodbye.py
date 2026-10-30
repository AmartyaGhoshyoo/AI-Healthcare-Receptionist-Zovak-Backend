"""Calls end with a warm goodbye when the caller is finished, but never mid-booking."""
import asyncio
from datetime import date, timedelta

import pytest

from tests.test_phone_and_forms import T, instant_speech, never, web_booked  # noqa: F401
from zovak.services.mock_scheduler import DemoScheduler
from zovak.voice.confirmation import is_goodbye
from zovak.voice.sideband import VoiceSideband


@pytest.mark.parametrize('said', [
    'bye', 'Okay, thanks, bye!', "Thank you, that's all.", 'I have to go now', 'You can end the call',
    'Please hang up', 'No that is it, goodbye', 'take care', 'cut the call please', 'okay I am done thanks',
])
def test_clear_goodbyes(said):
    assert is_goodbye(said)


@pytest.mark.parametrize('said', [
    'okay', 'thanks', 'no', 'Bye, but first move it to 3', 'Can you book me tomorrow? bye',
    'What time is Dr Rahman free', 'Is that it?', 'yes confirm it',
])
def test_not_goodbyes(said):
    assert not is_goodbye(said)


def new_call():
    s = DemoScheduler()
    voice = VoiceSideband(session_id='s', conversation_id='g', api_key='unused', scheduler=s,
                          agent_model='unused')
    return s, voice


@pytest.mark.asyncio
async def test_goodbye_without_a_booking():
    _, voice = new_call()
    reply = await voice._handle_turn('Okay thanks, bye', never)
    assert reply == 'Thanks for calling Brightside Family Clinic. Take care, goodbye!'
    await asyncio.gather(*voice._pending)
    assert voice.call_ended == 'done'


@pytest.mark.asyncio
async def test_goodbye_after_booking_names_the_appointment():
    _, voice, _, _ = await web_booked()
    reply = await voice._handle_turn('great, bye', never)
    assert reply.startswith("You're all set with Dr.") and ' at ' in reply and voice.guard.ending


@pytest.mark.asyncio
async def test_half_done_booking_asks_once_then_leaves_nothing_booked():
    s, voice = new_call()
    free = await s.available_slots(T, 'consultation', date.today() + timedelta(days=3))

    async def propose(_):
        await s.propose(T, 'g', 'consultation', free[0].start, 'G')
        return 'p'

    await voice._handle_turn('Book the first one', propose)
    first = await voice._handle_turn('I have to go', never)
    assert "isn't booked yet" in first and not voice.guard.ending
    second = await voice._handle_turn('bye', never)
    assert "haven't booked anything" in second and voice.guard.ending
    assert await s.latest_pending(T, 'g') is None and not s._bookings


@pytest.mark.asyncio
async def test_half_done_move_asks_once_then_keeps_the_appointment():
    s, voice, free, _ = await web_booked()
    booking = await s.latest_booking(T, 'w')

    async def move(_):
        await s.propose_move(T, 'w', free[4].start)
        return 'prepared'

    await voice._handle_turn('move it', move)
    assert "change isn't done" in await voice._handle_turn('bye', never)
    assert 'stays as it was' in await voice._handle_turn('bye', never)
    assert (await s.latest_booking(T, 'w')).start == booking.start


@pytest.mark.asyncio
async def test_goodbye_the_voice_model_kept_to_itself_still_ends_the_call():
    _, voice = new_call()
    voice.GOODBYE_WAIT = 0.05
    for i, words in enumerate(['Okay, ', 'thanks, ', 'bye!']):
        voice.transcripts.ingest({'type': 'session.input_transcript.delta', 'delta': words, 'end_ms': 100 + i})
        voice._watch_for_goodbye()
    await asyncio.sleep(0.2)
    assert voice.guard.ending
    await asyncio.gather(*voice._pending)
    assert voice.call_ended == 'done'


@pytest.mark.asyncio
async def test_words_already_handed_to_zovak_are_not_checked_twice():
    _, voice = new_call()
    voice.GOODBYE_WAIT = 0.05
    voice.transcripts.ingest({'type': 'session.input_transcript.delta', 'delta': 'bye', 'end_ms': 100})
    voice.transcripts.take_for_delegation(200)  # a delegation took these words
    voice._watch_for_goodbye()
    await asyncio.sleep(0.2)
    assert not voice.guard.ending
