"""WhatsApp call experiment: Meta's SDP offer → GPT-Live → SDP answer back to Meta."""
from types import SimpleNamespace

import pytest

from zovak.providers.notifications.sms_router import SmsRouter
from zovak.services.booking_forms import BookingForms
from zovak.services.mock_scheduler import DemoScheduler
from zovak.services.whatsapp_calls import WhatsAppCalls, WhatsAppOrSms
from zovak.voice.sideband import VoiceSideband
import zovak.api.routes.phone as phone


class FakeCloud:
    def __init__(self, fail_text=False):
        self.actions, self.texts, self.fail_text = [], [], fail_text

    async def call_action(self, call_id, action, sdp=None):
        self.actions.append((call_id, action, sdp))

    async def send_text(self, to, body):
        if self.fail_text:
            raise RuntimeError('outside the 24h window')
        self.texts.append((to, body))


class FakeOpenAI:
    def __init__(self):
        self.created = []

        async def create(*, session, transport):
            self.created.append((session, transport))
            return SimpleNamespace(session=SimpleNamespace(id='live_wa1'),
                                   transport=SimpleNamespace(sdp='v=0 answer'))
        self.live = SimpleNamespace(create=create)


def connect(call_id='wacid.1', sdp='v=0 offer'):
    return {'entry': [{'changes': [{'field': 'calls', 'value': {'calls': [
        {'id': call_id, 'event': 'connect', 'direction': 'USER_INITIATED', 'from': '8801712345678',
         'session': {'sdp_type': 'offer', 'sdp': sdp}}]}}]}]}


@pytest.fixture
def calls(monkeypatch):
    monkeypatch.setattr(VoiceSideband, 'start', lambda self: None)
    phone._recent_calls.clear()
    scheduler = DemoScheduler()
    state = SimpleNamespace(
        settings=SimpleNamespace(openai_api_key='sk', openai_agent_model='m', public_base_url='https://x',
                                 max_calls_per_caller_per_hour=5),
        scheduler=scheduler, booking_forms=BookingForms(scheduler), sms=SmsRouter(),
    )
    cloud, openai = FakeCloud(), FakeOpenAI()
    return WhatsAppCalls(state, cloud, openai_client=openai, bridge_factory=FakeBridge), cloud, openai


class FakeBridge:
    def __init__(self, label):
        self.calls = []
        self.stats = {'caller→nofa': 0, 'nofa→caller': 0}

    async def openai_offer(self):
        return 'v=0 bridge offer to openai'

    async def openai_answer(self, sdp):
        self.calls.append(('openai_answer', sdp))

    async def meta_answer(self, offer):
        self.calls.append(('meta_answer', offer))
        return 'v=0 bridge answer to meta'

    def start_stats(self):
        pass

    async def close(self):
        self.calls.append(('close',))


@pytest.mark.asyncio
async def test_bridge_connects_both_legs(calls):
    wa, cloud, openai = calls
    await wa.handle_payload(connect())
    session, transport = openai.created[0]
    # GPT-Live gets the bridge's own offer (not Meta's), Meta gets the bridge's answer.
    assert transport == {'type': 'webrtc', 'sdp': 'v=0 bridge offer to openai'}
    assert 'WhatsApp' in session['instructions'] and session['delegation'] == {'type': 'client'}
    bridge = wa.bridges['wacid.1']
    assert bridge.calls == [('openai_answer', 'v=0 answer'), ('meta_answer', 'v=0 offer')]
    assert cloud.actions == [('wacid.1', 'pre_accept', 'v=0 bridge answer to meta'),
                             ('wacid.1', 'accept', 'v=0 bridge answer to meta')]
    handler = wa.by_call['wacid.1']
    assert handler.channel == 'phone' and handler.caller == '+8801712345678'
    assert handler.conversation_id == 'wa-8801712345678'  # same as this person's WhatsApp chat
    await wa.handle_payload(connect())  # duplicate webhook: ignored
    assert len(openai.created) == 1


@pytest.mark.asyncio
async def test_form_link_goes_by_whatsapp_with_sms_fallback():
    router = SmsRouter()
    ok = WhatsAppOrSms(FakeCloud(), router)
    await ok.send('+8801712345678', 'form https://x/form/abc')
    assert ok.cloud.texts == [('8801712345678', 'form https://x/form/abc')] and len(router.messages) == 1
    failing = WhatsAppOrSms(FakeCloud(fail_text=True), router)
    await failing.send('+8801712345678', 'again')
    assert router.messages[-1].body == 'again'  # fell back to the SMS router


def test_sdp_summary_keeps_connection_lines():
    from zovak.services.whatsapp_calls import sdp_summary
    sdp = ("v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=ice-lite\r\na=setup:actpass\r\n"
           "a=fingerprint:sha-256 AA\r\na=candidate:1 1 udp 1 1.2.3.4 3478 typ host\r\na=sendrecv\r\n")
    summary = sdp_summary(sdp)
    assert 'a=ice-lite' in summary and 'a=setup:actpass' in summary
    assert "candidates=1 types=['host']" in summary and 'fingerprint=yes' in summary


@pytest.mark.asyncio
async def test_hangup_closes_the_bridge(calls):
    wa, cloud, openai = calls
    await wa.handle_payload(connect())
    bridge = wa.bridges['wacid.1']
    await wa.handle_payload({'entry': [{'changes': [{'field': 'calls', 'value': {'calls': [
        {'id': 'wacid.1', 'event': 'terminate', 'status': 'COMPLETED'}]}}]}]})
    assert ('close',) in bridge.calls and 'wacid.1' not in wa.bridges


@pytest.mark.asyncio
async def test_relay_track_sends_silence_then_forwards_audio():
    from zovak.voice.webrtc_bridge import RelayTrack, silence
    track = RelayTrack()
    first = await track.recv()  # nothing from the other leg yet: silence keeps media flowing
    assert first.samples == 960 and track.forwarded == 0
    for _ in range(12):  # backlog is capped so latency can't build up
        track.push(silence())
    assert track.queue.qsize() == 10
    second = await track.recv()
    assert track.forwarded == 1 and second.pts == 960


@pytest.mark.asyncio
async def test_call_and_chat_share_bookings(calls):
    """A booking made in WhatsApp chat is visible (and movable) from a WhatsApp call."""
    from datetime import date, timedelta
    from zovak.services.booking_state import booking_status_for_agent
    wa, cloud, openai = calls
    scheduler = wa.state.scheduler
    free = await scheduler.available_slots('demo-business', 'consultation', date.today() + timedelta(days=3))
    chat = 'wa-8801712345678'  # what WhatsAppBot uses for this person's chat
    p = await scheduler.propose('demo-business', chat, 'consultation', free[0].start, 'A', 'a@example.com')
    await scheduler.confirm('demo-business', chat, p.id)
    await wa.handle_payload(connect())
    call_conversation = wa.by_call['wacid.1'].conversation_id
    assert (await booking_status_for_agent(scheduler, 'demo-business', call_conversation)).startswith('CONFIRMED')
