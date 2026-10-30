"""Silence timeout and off-topic strikes end a web voice call politely."""
import asyncio

import pytest

import zovak.voice.sideband as sideband_module
from zovak.services.mock_scheduler import DemoScheduler
from zovak.voice.call_guard import CallGuard
from zovak.voice.sideband import VoiceSideband


def test_silence_warns_at_10s_then_ends_at_20s():
    guard = CallGuard()
    actions = [guard.tick(1, 1000, busy=False, email_open=False) for _ in range(20)]
    assert actions[9] == 'check_in' and actions.count('check_in') == 1
    assert actions[19] == 'end' and actions[:19].count('end') == 0


def test_caller_activity_resets_and_speech_or_work_pauses():
    guard = CallGuard()
    for _ in range(15):
        guard.tick(1, 1000, busy=False, email_open=False)
    guard.caller_active()
    assert guard.idle == 0 and not guard.checked_in
    guard.agent_speaking(now=1000, seconds=10)
    assert guard.tick(1, 1005, busy=False, email_open=False) is None and guard.idle == 0
    assert guard.tick(1, 1020, busy=True, email_open=False) is None and guard.idle == 0


def test_email_box_gets_longer_limit():
    guard = CallGuard()
    actions = [guard.tick(1, 1000, busy=False, email_open=True) for _ in range(120)]
    assert 'end' not in actions[:119] and actions[119] == 'end'


def voice_call():
    return VoiceSideband(session_id='s', conversation_id='c', api_key='unused',
                         scheduler=DemoScheduler(), agent_model='unused')


@pytest.mark.asyncio
async def test_third_off_topic_request_ends_the_call(monkeypatch):
    monkeypatch.setattr(sideband_module, 'speech_seconds', lambda _: 0)
    voice = voice_call()
    voice.HANGUP_GRACE = 0

    async def off_topic_agent(_):
        voice.report_off_topic()  # what the agent's report_off_topic tool does
        return 'model text that must not be used'

    first = await voice._handle_turn('Tell me a joke', off_topic_agent)
    second = await voice._handle_turn('What is the capital of France?', off_topic_agent)
    assert 'only help with appointments' in first and 'Would you like to book' in first
    assert "I'll end the call" in second and voice.call_ended is None
    third = await voice._handle_turn('How much does it cost?', off_topic_agent)
    assert 'Goodbye' in third
    await asyncio.gather(*voice._pending)
    assert voice.call_ended == 'off_topic'
    assert (await voice.booking_state())['call_ended'] == 'off_topic'


@pytest.mark.asyncio
async def test_on_topic_turns_are_not_counted():
    voice = voice_call()

    async def helpful_agent(_):
        return 'We are open 9 to 5.'

    assert await voice._handle_turn('When are you open?', helpful_agent) == 'We are open 9 to 5.'
    assert voice.guard.off_topic == 0


@pytest.mark.asyncio
async def test_silence_watchdog_says_goodbye_and_flags_hangup(monkeypatch):
    monkeypatch.setattr(sideband_module, 'speech_seconds', lambda _: 0)
    voice = voice_call()
    voice.HANGUP_GRACE = 0
    voice.guard = CallGuard(silence_limit=2, check_in_before=1)
    voice._heard_caller = True  # the caller's audio has arrived
    spoken = []

    async def speak(message):
        spoken.append(message)

    voice._speak = speak
    await asyncio.wait_for(voice._watch_silence(), timeout=6)
    assert 'still there' in spoken[0]
    assert "haven't heard from you" in spoken[-1]
    assert voice.call_ended == 'silence'


@pytest.mark.asyncio
async def test_no_caller_audio_ends_without_silence_warnings(monkeypatch):
    """A broken audio path (e.g. SRTP/network) is not caller silence: no warnings, one log."""
    monkeypatch.setattr(sideband_module, 'speech_seconds', lambda _: 0)
    monkeypatch.setattr(sideband_module, '_NO_AUDIO', 1)
    voice = voice_call()
    voice.HANGUP_GRACE = 0
    voice._connected_at = sideband_module.monotonic()
    spoken = []

    async def speak(message):
        spoken.append(message)

    voice._speak = speak
    await asyncio.wait_for(voice._watch_silence(), timeout=6)
    assert spoken == [] and voice.call_ended == 'no_audio'


@pytest.mark.parametrize('text,meaningful', [
    ('I want to book a consultation', True), ('yes', True), ('no', True), ('ok', True),
    ('uh', False), ('hmm', False), ('um... uh', False), ('[cough]', False),
    ('(sniffs)', False), ('*clears throat*', False), ('mm hmm', False), ('', False),
    ('আমি বুক করতে চাই', True), ('25', True),
])
def test_meaningful_speech(text, meaningful):
    from zovak.voice.confirmation import is_meaningful
    assert is_meaningful(text) is meaningful


def test_coughs_and_fillers_do_not_reset_the_silence_timer():
    voice = voice_call()
    for _ in range(12):  # past the 10 s warning
        voice.guard.tick(1, 1000, busy=False, email_open=False)
    assert voice.guard.checked_in
    voice._caller_said('[cough]')
    voice._caller_said('uh')
    assert voice.guard.idle == 12 and voice.guard.checked_in  # still counting
    voice._caller_said('sorry I am here')
    assert voice.guard.idle == 0 and not voice.guard.checked_in


@pytest.mark.asyncio
async def test_noise_only_turn_after_warning_ends_the_call(monkeypatch):
    monkeypatch.setattr(sideband_module, 'speech_seconds', lambda _: 0)
    voice = voice_call()
    voice.HANGUP_GRACE = 0
    assert "didn't catch" in voice._noise_only_turn()  # before any warning: ask again
    voice.guard.checked_in = True  # the 10 s warning was given
    goodbye = voice._noise_only_turn()  # ...and they only cough
    assert 'not hearing a request' in goodbye and voice.guard.ending
    await asyncio.gather(*voice._pending)
    assert voice.call_ended == 'noise'


@pytest.mark.asyncio
async def test_three_noise_only_turns_end_the_call(monkeypatch):
    monkeypatch.setattr(sideband_module, 'speech_seconds', lambda _: 0)
    voice = voice_call()
    voice.HANGUP_GRACE = 0
    replies = [voice._noise_only_turn() for _ in range(3)]
    assert "didn't catch" in replies[0] and "didn't catch" in replies[1]
    assert 'not hearing a request' in replies[2]
    await asyncio.gather(*voice._pending)
