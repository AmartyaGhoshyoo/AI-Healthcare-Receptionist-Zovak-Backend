"""Typed email UI gate / voice consent integration regression tests."""
from datetime import date, timedelta
from types import SimpleNamespace
import json
import time

import pytest
import httpx
from fastapi import FastAPI
from zovak.services.mock_scheduler import DemoScheduler, SchedulingError
from zovak.services.google_scheduler import GoogleBackedScheduler
from zovak.voice.sideband import VoiceSideband
from zovak.voice.email_form import validate_typed_email
from zovak.api.routes.voice_dev import router


def test_email_preserves_local_case_and_rejects_bad_inputs():
    assert validate_typed_email('Amartya40+VIP@GMail.COM') == 'Amartya40+VIP@gmail.com'
    for invalid in ('a@example', 'a@@example.com', 'a@example..com', 'a@-bad.com',
                    'a b@example.com', ' a@example.com', 'é@example.com',
                    'a..b@example.com', 'a@example.com\n'):
        with pytest.raises(ValueError):
            validate_typed_email(invalid)


async def setup(scheduler=None, conversation_id='form-1', *, with_email=None):
    scheduler = scheduler or DemoScheduler()
    voice = VoiceSideband(session_id='sess-1', conversation_id=conversation_id,
                          api_key='unused', scheduler=scheduler, agent_model='unused')
    slots = await scheduler.available_slots('demo-business', 'consultation', date.today()+timedelta(days=5))
    async def make(_):
        await scheduler.propose('demo-business', conversation_id, 'consultation',
                                slots[0].start, 'Demo Customer', customer_email=with_email)
        return 'proposed'
    msg = await voice._handle_turn('Book', make)
    return scheduler, voice, msg


@pytest.mark.asyncio
async def test_form_controls_booking_and_never_confirms_itself():
    scheduler, voice, msg = await setup()
    assert 'box on your screen' in msg
    state = await voice.email_state()
    assert state['state'] == 'email_required'
    rid = state['email_request']['request_id']
    assert len(rid) > 20
    proposal = await scheduler.latest_pending('demo-business', voice.conversation_id)
    async def forbidden(_):
        raise AssertionError('No agent needed to confirm')
    response = await voice._handle_turn('Yes, confirm my appointment', forbidden)
    assert 'enter your email' in response
    assert not scheduler._bookings
    with pytest.raises(SchedulingError):
        await voice.submit_email(request_id='outdated-request', email='client@example.com')
    with pytest.raises(ValueError):
        await voice.submit_email(request_id=rid, email='not an email')
    assert not proposal.customer_email
    done = await voice.submit_email(request_id=rid, email='Amartya40@Example.COM')
    assert done['state'] == 'verified'
    assert done['booking_confirmed'] is False
    assert done['email'] == 'Amartya40@example.com'
    assert 'Are you sure' in done['assistant_message']
    assert (await voice.email_state())['state'] == 'verified'
    assert not scheduler._bookings
    # No live WebSocket in tests: backend forces one readback turn.
    said = await voice._handle_turn('I entered my email', forbidden)
    assert 'appointment details' in said
    assert not scheduler._bookings
    response = await voice._handle_turn('Yes, confirm my appointment', forbidden)
    assert 'now confirmed' in response
    assert proposal.id in scheduler._bookings
    assert scheduler._bookings[proposal.id].customer_email == 'Amartya40@example.com'
    assert (await voice.email_state())['state'] == 'idle'


@pytest.mark.asyncio
async def test_email_correction_reopens_same_proposal():
    scheduler, voice, _ = await setup()
    rid = voice.email_form.request_id
    await voice.submit_email(request_id=rid, email='bad@example.com')
    async def forbidden(_): raise AssertionError('no agent')
    await voice._handle_turn('Email submitted', forbidden)
    response = await voice._handle_turn('Wait, the email is wrong', forbidden)
    assert 'reopened the email box' in response
    rid2 = voice.email_form.request_id
    assert rid2 != rid
    assert (await voice.email_state())['state'] == 'email_required'
    with pytest.raises(SchedulingError):
        await voice.submit_email(request_id=rid, email='bad@example.com')
    await voice.submit_email(request_id=rid2, email='good@example.com')
    assert not scheduler._bookings
    proposal = await scheduler.latest_pending('demo-business', voice.conversation_id)
    assert proposal.customer_email == 'good@example.com'


@pytest.mark.asyncio
async def test_session_token_and_route_contract():
    scheduler, voice, _ = await setup()
    app = FastAPI()
    app.include_router(router)
    app.state.voice_sidebands = {voice.session_id: voice}
    app.state.scheduler = scheduler
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as client:
        path='/api/v1/dev/voice/email-input'
        params={'conversation_id':voice.conversation_id}
        assert (await client.get(path,params=params)).status_code == 401
        assert (await client.get(path,params=params,headers={'Authorization':'Bearer wrong'})).status_code == 403
        headers={'Authorization':f'Bearer {voice.ui_token}'}
        r=await client.get(path,params=params,headers=headers)
        assert r.status_code==200
        request_id=r.json()['email_request']['request_id']
        payload={'conversation_id':voice.conversation_id,'request_id':request_id,'email':'demo@example.com'}
        assert (await client.post(path,json={**payload,'request_id':'invalid'},headers=headers)).status_code==409
        r=await client.post(path,json=payload,headers=headers)
        assert r.status_code==200
        assert r.json()['booking_confirmed'] is False
        assert (await client.get(path,params=params,headers=headers)).json()['state']=='verified'


@pytest.mark.asyncio
async def test_google_event_only_after_form_email_and_distinct_voice_approval():
    created=[]
    def responder(request):
        if request.url.path.endswith('/freeBusy'):
            return httpx.Response(200,json={'calendars': {'primary': {'busy':[]}}})
        if request.method=='GET' and '/events/' in request.url.path:
            return httpx.Response(404)
        if request.method=='POST' and request.url.path.endswith('/events'):
            created.append(json.loads(request.content))
            return httpx.Response(200,json={'id': created[-1]['id'],'status':'confirmed'})
        return httpx.Response(400)
    app=SimpleNamespace(state=SimpleNamespace(google_oauth_tokens={
        'access_token':'fake','expires_at':time.time()+3600,'refresh_token':'notneeded'}))
    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as google_client:
        scheduler=GoogleBackedScheduler(app,google_client=google_client)
        scheduler,voice,_=await setup(scheduler=scheduler,conversation_id='google-test')
        async def no(_): raise AssertionError('no llm')
        assert not created
        await voice._handle_turn('Yes, confirm my appointment',no)
        assert not created
        await voice.submit_email(request_id=voice.email_form.request_id,email='user@example.com')
        assert not created
        await voice._handle_turn('I entered my email',no)
        assert not created
        result=await voice._handle_turn('Yes, confirm my appointment',no)
        assert 'Google Calendar created' in result
        assert len(created)==1
        assert created[0]['attendees']==[{'email':'user@example.com'}]

@pytest.mark.asyncio
async def test_form_submit_can_prompt_live_voice_without_a_second_user_turn():
    scheduler, voice, _ = await setup(conversation_id='live-form-test')
    class FakeSocket:
        def __init__(self): self.sent=[]
        async def send(self, data): self.sent.append(json.loads(data))
    socket=FakeSocket()
    voice._socket=socket
    result=await voice.submit_email(request_id=voice.email_form.request_id,email='typed@example.com')
    assert result['booking_confirmed'] is False
    assert voice.needs_readback is False
    assert socket.sent[0]['type']=='session.commentary.append'
    assert socket.sent[0]['delegation_id'] is None
    assert 'appointment details' in socket.sent[0]['content']
    assert 'lowercase' not in socket.sent[0]['content']
    assert not scheduler._bookings
    async def never(_): raise AssertionError('No agent during confirmation')
    response=await voice._handle_turn('Yes, confirm my appointment',never)
    assert 'now confirmed' in response


@pytest.mark.asyncio
async def test_expired_proposal_cannot_accept_email():
    from datetime import datetime, timezone
    scheduler, voice, _ = await setup(conversation_id='expired-form-test')
    proposal=await scheduler.latest_pending('demo-business','expired-form-test')
    proposal.expires_at = datetime(2000,1,1,tzinfo=timezone.utc)
    assert (await voice.email_state())['state']=='idle'
    with pytest.raises(SchedulingError,match='No active email request'):
        await voice.submit_email(request_id=voice.email_form.request_id,email='person@example.com')
