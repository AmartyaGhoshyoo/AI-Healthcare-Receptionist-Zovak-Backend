"""Phone calls (SIP), the SMS booking form, safe spoken yes, and call-ending guardrails."""
import asyncio
import base64
import hashlib
import hmac
import json
import time
from datetime import date, datetime, timedelta, timezone
from time import monotonic
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import zovak.api.routes.phone as phone
import zovak.voice.sideband as sideband_module
from zovak.api.routes.forms import router as forms_router
from zovak.main import _guard_public_host
from zovak.providers.notifications.sms_outbox import DevSmsOutbox
from zovak.services.booking_forms import BookingForms
from zovak.services.mock_scheduler import DemoScheduler, SchedulingError
from zovak.voice.sideband import VoiceSideband

T = 'demo-business'


async def never(_):
    raise AssertionError('The agent must not be called for this step')


@pytest.fixture(autouse=True)
def instant_speech(monkeypatch):
    monkeypatch.setattr(sideband_module, 'speech_seconds', lambda _: 0)


async def phone_call():
    scheduler = DemoScheduler()
    hung_up = []

    async def hang_up():
        hung_up.append(True)

    voice = VoiceSideband(
        session_id='live_1', conversation_id='phone-1', api_key='unused', scheduler=scheduler,
        agent_model='unused', channel='phone', caller='+8801712345678',
        booking_forms=BookingForms(scheduler, minutes=2), sms=DevSmsOutbox(),
        public_base_url='https://demo.ngrok.app', hang_up=hang_up,
    )
    free = await scheduler.available_slots(T, 'consultation', date.today() + timedelta(days=3))

    async def propose(_):
        await scheduler.propose(T, 'phone-1', 'consultation', free[2].start, 'Amartya')
        return 'proposed'

    return scheduler, voice, free, propose, hung_up


# ── Phone: spoken "confirm" only sends the form ──────────────────────────────

@pytest.mark.asyncio
async def test_phone_confirm_sends_form_then_hangs_up_without_booking():
    scheduler, voice, free, propose, hung_up = await phone_call()
    question = await voice._handle_turn('Book a consultation at 10', propose)
    assert "say 'confirm'" in question and 'screen' not in question
    assert "say 'confirm'" in await voice._handle_turn('yes', never)  # bare yes: asks again
    reply = await voice._handle_turn('Uh, yes, confirm it', never)
    assert 'sent you a text' in reply and 'change the slot or cancel' in reply
    assert voice.guard.ending
    await asyncio.gather(*voice._pending)
    assert voice.call_ended == 'form_sent' and hung_up == [True]
    assert not scheduler._bookings  # nothing booked until the form is submitted
    sms = voice.sms.messages[-1]
    assert sms.to == '+8801712345678' and 'https://demo.ngrok.app/form/' in sms.body


@pytest.mark.asyncio
async def test_form_confirms_with_another_slot_once():
    scheduler, voice, free, propose, _ = await phone_call()
    await voice._handle_turn('Book', propose)
    await voice._handle_turn('confirm', never)
    forms = voice.booking_forms
    token = voice.sms.messages[-1].body.split('/form/')[1].split()[0]
    with pytest.raises(ValueError):
        await forms.submit(token, action='confirm', name='Amartya', email='nope')
    form = await forms.submit(token, action='confirm', name='Amartya Ghosh',
                              email='a@example.com', start_iso=free[5].start.isoformat())
    booking = scheduler._bookings[form.booking_id]
    assert booking.start == free[5].start and booking.customer_name == 'Amartya Ghosh'
    assert booking.customer_email == 'a@example.com'
    with pytest.raises(SchedulingError, match='already used'):
        await forms.submit(token, action='cancel')


@pytest.mark.asyncio
async def test_form_cancel_and_expiry():
    scheduler, voice, free, propose, _ = await phone_call()
    await voice._handle_turn('Book', propose)
    await voice._handle_turn('confirm', never)
    token = voice.sms.messages[-1].body.split('/form/')[1].split()[0]
    await voice.booking_forms.submit(token, action='cancel')
    assert await scheduler.latest_pending(T, 'phone-1') is None and not scheduler._bookings

    forms = BookingForms(scheduler)
    proposal = await scheduler.propose(T, 'phone-2', 'consultation', free[6].start, 'B')
    form = await forms.create(T, proposal, '+880')
    form.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(SchedulingError, match='expired'):
        await forms.submit(form.token, action='confirm', name='B', email='b@example.com')


@pytest.mark.asyncio
async def test_form_pages():
    scheduler = DemoScheduler()
    app = FastAPI()
    app.include_router(forms_router)
    app.state.booking_forms = forms = BookingForms(scheduler)
    free = await scheduler.available_slots(T, 'consultation', date.today() + timedelta(days=3))
    proposal = await scheduler.propose(T, 'p', 'consultation', free[1].start, 'Rae <b>')
    form = await forms.create(T, proposal, '+880')
    with TestClient(app) as client:
        page = client.get(f'/form/{form.token}').text
        assert 'id="timer"' in page and 'Rae &lt;b&gt;' in page and 'Confirm appointment' in page
        done = client.post(f'/form/{form.token}', content='action=confirm&name=Rae&email=r%40example.com'
                           f'&start={free[1].start.isoformat().replace("+", "%2B")}',
                           headers={'content-type': 'application/x-www-form-urlencoded'})
        assert "booked ✓" in done.text
        again = client.post(f'/form/{form.token}', content='action=cancel',
                            headers={'content-type': 'application/x-www-form-urlencoded'})
        assert again.status_code == 409 and 'already used' in again.text
        assert client.get('/form/not-a-token').status_code == 404


# ── A spoken yes is only acted on after a moment of silence ──────────────────

@pytest.mark.asyncio
async def test_yes_followed_by_more_speech_is_not_acted_on():
    scheduler, voice, free, propose, _ = await phone_call()
    voice.SETTLE_SECONDS = 0.3
    await voice._handle_turn('Book', propose)
    voice._turn_heard_at = monotonic()

    async def keeps_talking():
        await asyncio.sleep(0.1)
        voice._last_input_at = monotonic()  # "...but change the time"

    asyncio.create_task(keeps_talking())
    reply = await voice._handle_turn('yes confirm', never)
    assert 'Nothing has changed' in reply and not voice.sms.messages and not voice.guard.ending
    voice._turn_heard_at = monotonic()  # this time the caller stays quiet
    assert 'sent you a text' in await voice._handle_turn('yes confirm', never)


# ── Ending: "that's all", misuse ─────────────────────────────────────────────

async def web_booked():
    scheduler = DemoScheduler()
    voice = VoiceSideband(session_id='s', conversation_id='w', api_key='unused',
                          scheduler=scheduler, agent_model='unused')
    voice.HANGUP_GRACE = 0
    free = await scheduler.available_slots(T, 'consultation', date.today() + timedelta(days=3))

    async def propose(_):
        await scheduler.propose(T, 'w', 'consultation', free[0].start, 'C')
        return 'p'

    await voice._handle_turn('Book', propose)
    state = await voice.booking_state()
    await voice.submit_email(request_id=state['email_request_id'], email='c@example.com')
    result = await voice.confirm_booking(proposal_id=state['proposal']['id'])
    return scheduler, voice, free, result


@pytest.mark.asyncio
async def test_nothing_else_ends_the_call():
    scheduler, voice, free, result = await web_booked()
    assert 'anything else' in result['message']
    goodbye = await voice._handle_turn("No, that's all, thanks", never)
    assert "You're all set" in goodbye and 'goodbye' in goodbye
    assert (await voice.booking_state())['ending'] is True
    await asyncio.gather(*voice._pending)
    assert voice.call_ended == 'done'


@pytest.mark.asyncio
async def test_agent_can_end_call_when_customer_says_bye():
    scheduler, voice, free, _ = await web_booked()

    async def agent_hears_bye(_):
        voice.request_end()  # the agent's end_call tool
        return 'bye'

    # Wording the goodbye check doesn't know: the agent's end_call still ends it warmly.
    reply = await voice._handle_turn("I think we're finished here", agent_hears_bye)
    assert "You're all set" in reply and voice.guard.ending


@pytest.mark.asyncio
async def test_booking_moving_cancelling_over_and_over_ends_the_call():
    scheduler, voice, free, _ = await web_booked()
    voice.guard.action_limit = 2  # the 3rd prepared move in this call ends it
    replies = []
    for i in range(3, 6):
        async def agent_moves(_, slot=free[i]):
            await scheduler.propose_move(T, 'w', slot.start)
            return 'prepared'
        replies.append(await voice._handle_turn('move it again', agent_moves))
        if voice.guard.ending:
            break
        await voice._handle_turn('no keep it', never)
    assert 'a lot of changes' in replies[-1] and voice.guard.ending
    assert await scheduler.latest_change(T, 'w') is None  # the last one was dropped
    await asyncio.gather(*voice._pending)
    assert voice.call_ended == 'misuse'


@pytest.mark.asyncio
async def test_agent_reported_misuse_ends_immediately():
    scheduler, voice, free, _ = await web_booked()

    async def troll(_):
        voice.report_misuse()
        return 'whatever'

    assert 'a lot of changes' in await voice._handle_turn('book cancel book cancel lol', troll)


# ── Phone webhook ────────────────────────────────────────────────────────────

SECRET = 'whsec_' + base64.b64encode(b'test-secret-123').decode()


def signed(body: dict, event_id='evt_1'):
    raw = json.dumps(body)
    ts = str(int(time.time()))
    sig = base64.b64encode(hmac.new(b'test-secret-123', f'{event_id}.{ts}.{raw}'.encode(),
                                    hashlib.sha256).digest()).decode()
    return raw, {'webhook-id': event_id, 'webhook-timestamp': ts,
                 'webhook-signature': f'v1,{sig}', 'content-type': 'application/json'}


def incoming(session_id='live_abc', caller='"Amartya" <sip:+8801712345678@carrier.example>',
             event_id='evt_1'):
    return {'id': event_id, 'object': 'event', 'created_at': int(time.time()),
            'type': 'live.transport.incoming',
            'data': {'session_id': session_id, 'type': 'sip',
                     'sip_headers': [{'name': 'From', 'value': caller}]}}


@pytest.fixture
def phone_app(monkeypatch):
    from openai import AsyncOpenAI

    calls = []

    class Sessions:
        async def accept(self, session_id, *, session):
            calls.append(('accept', session_id, session))

        async def reject(self, session_id, *, status_code):
            calls.append(('reject', session_id, status_code))

        async def hangup(self, session_id):
            calls.append(('hangup', session_id))

    def client(key):
        real = AsyncOpenAI(api_key='x')
        real.__dict__['live'] = SimpleNamespace(sessions=Sessions())
        return real

    monkeypatch.setattr(phone, 'openai_client', client)
    monkeypatch.setattr(VoiceSideband, 'start', lambda self: None)  # no live socket in tests
    phone._seen_events.clear()
    phone._recent_calls.clear()
    app = FastAPI()
    app.include_router(phone.router)
    app.state.settings = SimpleNamespace(
        openai_webhook_secret=SECRET, openai_api_key='sk-test', openai_agent_model='m',
        public_base_url='https://demo.ngrok.app', max_calls_per_caller_per_hour=2,
    )
    app.state.scheduler = DemoScheduler()
    app.state.booking_forms = BookingForms(app.state.scheduler)
    app.state.sms = DevSmsOutbox()
    return app, calls


def test_webhook_accepts_signed_call_and_attaches_phone_sideband(phone_app):
    app, calls = phone_app
    raw, headers = signed(incoming())
    with TestClient(app) as client:
        assert client.post('/api/v1/phone/openai-webhook', content=raw, headers=headers).json()['accepted']
        again = client.post('/api/v1/phone/openai-webhook', content=raw, headers=headers).json()
    assert again['duplicate']
    kind, session_id, session = calls[0]
    assert (kind, session_id) == ('accept', 'live_abc')
    assert session['type'] == 'live' and 'PHONE call' in session['instructions']
    assert session['delegation'] == {'type': 'client'}
    handler = app.state.voice_sidebands['live_abc']
    assert handler.channel == 'phone' and handler.caller == '+8801712345678'


def test_webhook_rejects_bad_signature_and_rate_limits(phone_app):
    app, calls = phone_app
    raw, headers = signed(incoming())
    with TestClient(app) as client:
        bad = client.post('/api/v1/phone/openai-webhook', content=raw,
                          headers={**headers, 'webhook-signature': 'v1,bm9wZQ=='})
        assert bad.status_code == 400
        for i in range(3):
            raw, headers = signed(incoming(session_id=f'live_{i}', event_id=f'evt_{i}'),
                                  event_id=f'evt_{i}')
            result = client.post('/api/v1/phone/openai-webhook', content=raw, headers=headers).json()
    assert result['rejected'] == 'rate_limited'
    assert calls[-1] == ('reject', 'live_2', 486)


def test_caller_id_from_sip_headers():
    header = lambda v: [SimpleNamespace(name='From', value=v)]  # noqa: E731
    assert phone.caller_id(header('<sip:+8801712345678@x.example>;tag=1')) == '+8801712345678'
    assert phone.caller_id(header('"A" <sip:alice@sip.linphone.org>')) == 'alice@sip.linphone.org'
    assert phone.caller_id([]) == 'unknown'


# ── Public tunnel only reaches the webhook and forms ─────────────────────────

def test_public_host_reaches_only_webhook_and_forms():
    app = FastAPI()
    for path in ('/api/v1/dev/chat', '/form/abc', '/api/v1/phone/openai-webhook'):
        app.add_api_route(path, lambda: {'ok': True}, methods=['GET'])
    _guard_public_host(app, 'https://demo.ngrok.app')
    with TestClient(app) as client:
        public = {'host': 'demo.ngrok.app'}
        assert client.get('/api/v1/dev/chat', headers=public).status_code == 404
        assert client.get('/form/abc', headers=public).status_code == 200
        assert client.get('/api/v1/phone/openai-webhook', headers=public).status_code == 200
        forwarded = {'host': '127.0.0.1:8000', 'x-forwarded-for': '1.2.3.4'}
        assert client.get('/api/v1/dev/chat', headers=forwarded).status_code == 404
        assert client.get('/api/v1/dev/chat', headers={'host': '127.0.0.1:8000'}).status_code == 200


def test_deployed_website_reaches_its_routes_only_with_the_site_key():
    app = FastAPI()
    app.add_api_route('/api/v1/dev/chat', lambda: {'ok': True}, methods=['GET'])
    _guard_public_host(app, 'https://demo.ngrok.app', 'site-key-123')
    with TestClient(app) as client:
        public = {'host': 'demo.ngrok.app'}
        assert client.get('/api/v1/dev/chat', headers=public).status_code == 404
        assert client.get('/api/v1/dev/chat', headers={**public, 'x-zovak-site-key': 'wrong'}).status_code == 404
        assert client.get('/api/v1/dev/chat', headers={**public, 'x-zovak-site-key': 'site-key-123'}).status_code == 200
    app2 = FastAPI()
    app2.add_api_route('/api/v1/dev/chat', lambda: {'ok': True}, methods=['GET'])
    _guard_public_host(app2, 'https://demo.ngrok.app', '')  # no key set: never open, even with a header
    with TestClient(app2) as client:
        assert client.get('/api/v1/dev/chat', headers={'host': 'demo.ngrok.app', 'x-zovak-site-key': ''}).status_code == 404
