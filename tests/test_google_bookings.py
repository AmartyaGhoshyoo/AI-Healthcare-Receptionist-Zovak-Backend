import json
import time
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest

from zovak.providers.calendar.google_invites import GoogleInvites
from zovak.services.google_scheduler import GoogleBackedScheduler
from zovak.services.mock_scheduler import SchedulingError


def make_app():
    return SimpleNamespace(state=SimpleNamespace(google_oauth_tokens={
        'access_token': 'fake', 'expires_at': time.time() + 3500,
        'refresh_token': 'unused',
    }))


@pytest.mark.asyncio
async def test_busy_blocks_slot_and_other_slots_remain():
    app = make_app()
    seen = []
    def transport(request):
        seen.append(request)
        if request.url.path.endswith('freeBusy'):
            payload = json.loads(request.content)
            if len(payload['items']) == 1:
                return httpx.Response(200, json={'calendars': {'primary': {
                    'busy': [{'start': slot.start.isoformat(), 'end': slot.end.isoformat()}],
                }}})
        return httpx.Response(404)
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        scheduler = GoogleBackedScheduler(app, google_client=client)
        day = date.today() + timedelta(days=5)
        slot = (await super(GoogleBackedScheduler, scheduler).available_slots('demo-business', 'consultation', day))[2]
        slots = await scheduler.available_slots('demo-business', 'consultation', day)
        assert slot.start not in [s.start for s in slots]
        assert slots
    assert len(seen) == 1  # 1 freebusy request for the whole day


@pytest.mark.asyncio
async def test_requires_email_for_real_booking():
    app = make_app()
    scheduler = GoogleBackedScheduler(app)
    with pytest.raises(SchedulingError, match='email'):
        await scheduler.propose('demo-business','c1','consultation',None,'Test')


@pytest.mark.asyncio
async def test_real_confirm_creates_event_and_attendee_after_approval():
    app = make_app()
    requests = []
    saved = {}

    def transport(request):
        requests.append(request)
        if request.method == 'POST' and request.url.path.endswith('/freeBusy'):
            return httpx.Response(200, json={'calendars': {'primary': {'busy': []}}})
        if request.method == 'GET' and '/events/' in request.url.path:
            return httpx.Response(404)
        if request.method == 'POST' and request.url.path.endswith('/events'):
            assert request.url.params.get('sendUpdates') == 'all'
            body = json.loads(request.content)
            saved.update(body)
            return httpx.Response(200, json={'id': body['id'], 'status':'confirmed'})
        return httpx.Response(400)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        scheduler = GoogleBackedScheduler(app, google_client=client)
        day = date.today() + timedelta(days=5)
        slot = (await scheduler.available_slots('demo-business','consultation',day))[4]
        proposal = await scheduler.propose('demo-business','c2','consultation',slot.start,
                                           'Test Customer','customer@example.com')
        assert not saved  # pending proposal didn't create a real event
        assert proposal.id not in scheduler._bookings
        reply = await scheduler.confirm('demo-business','c2',proposal.id)
        assert reply.provider == 'google-calendar'
        assert saved['attendees'] == [{'email':'customer@example.com'}]
        assert saved['start']['dateTime'] == proposal.start.isoformat()
        assert proposal.id in scheduler._bookings
        assert (await scheduler.confirm('demo-business','c2',proposal.id)).booking_id == proposal.id
    assert len([r for r in requests if r.method == 'POST' and r.url.path.endswith('events')]) == 1


@pytest.mark.asyncio
async def test_insert_failure_does_not_claim_confirmed():
    app=make_app()
    def transport(request):
        if request.url.path.endswith('freeBusy'):
            return httpx.Response(200, json={'calendars': {'primary': {'busy': []}}})
        if request.method == 'GET':
            return httpx.Response(404)
        return httpx.Response(500, json={'error': 'fail'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        scheduler=GoogleBackedScheduler(app,google_client=client)
        day=date.today()+timedelta(days=5)
        slot=(await scheduler.available_slots('demo-business','consultation',day))[0]
        proposal=await scheduler.propose('demo-business','test','consultation',slot.start,'Client','client@example.com')
        with pytest.raises(SchedulingError,match='could not verify'):
            await scheduler.confirm('demo-business','test',proposal.id)
        assert proposal.status == 'pending'
        assert proposal.id not in scheduler._bookings


@pytest.mark.asyncio
async def test_no_credential_fails_closed():
    app=SimpleNamespace(state=SimpleNamespace())
    scheduler=GoogleBackedScheduler(app)
    with pytest.raises(SchedulingError, match='not connected'):
        await scheduler.available_slots('demo-business','consultation', date.today()+timedelta(days=5))

@pytest.mark.asyncio
async def test_voice_requires_explicit_approval_before_google_insert():
    from zovak.voice.sideband import VoiceSideband

    app=make_app()
    inserts=[]
    def transport(request):
        if request.url.path.endswith('/freeBusy'):
            return httpx.Response(200, json={'calendars': {'primary': {'busy': []}}})
        if request.method == 'GET':
            return httpx.Response(404)
        if request.method == 'POST' and request.url.path.endswith('/events'):
            data=json.loads(request.content)
            inserts.append(data)
            return httpx.Response(200, json={'id':data['id'],'status':'confirmed'})
        return httpx.Response(400)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        scheduler=GoogleBackedScheduler(app,google_client=client)
        day=date.today()+timedelta(days=5)
        slot=(await scheduler.available_slots('demo-business','consultation',day))[0]
        voice=VoiceSideband(session_id='test',conversation_id='client',api_key='unused',
                            scheduler=scheduler,agent_model='unused')
        async def create_proposal(_):
            await scheduler.propose('demo-business','client','consultation',slot.start,
                                    'Test Customer','customer@example.com')
            return 'Proposed'
        async def not_called(_):
            raise AssertionError('LLM cannot perform confirmed booking')
        summary=await voice._handle_turn('I want to book',create_proposal)
        assert 'customer@example.com' in summary
        assert 'Are you sure' in summary
        assert not inserts
        clarification=await voice._handle_turn('Yes',not_called)
        assert "haven't booked" in clarification
        assert not inserts
        reply=await voice._handle_turn('Yes, confirm my appointment',not_called)
        assert 'Google Calendar created the appointment' in reply
        assert len(inserts)==1
        assert inserts[0]['attendees']==[{'email':'customer@example.com'}]
