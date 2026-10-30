"""Confirm button, same-conversation move/cancel, reminders and date context."""
import json
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zovak.agents.receptionist import today_instructions
from zovak.api.routes.dev import router as chat_router
from zovak.services.google_scheduler import GoogleBackedScheduler, google_event_id
from zovak.services.mock_scheduler import DemoScheduler, SchedulingError
from zovak.voice.confirmation import classify_change_reply
from zovak.voice.sideband import VoiceSideband
from zovak.workers.reminders import ReminderWorker

T = 'demo-business'


async def never(_):
    raise AssertionError('The agent must not be called for this step')


async def slots(scheduler, day_offset=5):
    return await DemoScheduler.available_slots(
        scheduler, T, 'consultation', date.today() + timedelta(days=day_offset)
    )


async def voice_with_proposal(scheduler=None, conv='v1'):
    scheduler = scheduler or DemoScheduler()
    voice = VoiceSideband(session_id='s', conversation_id=conv, api_key='unused',
                          scheduler=scheduler, agent_model='unused')
    free = await scheduler.available_slots(T, 'consultation', date.today() + timedelta(days=5))

    async def make(_):
        await scheduler.propose(T, conv, 'consultation', free[0].start, 'Test Customer')
        return 'proposed'

    await voice._handle_turn('Book a consultation', make)
    return scheduler, voice, free


async def booked_voice(conv='v1'):
    scheduler, voice, free = await voice_with_proposal(conv=conv)
    state = await voice.booking_state()
    await voice.submit_email(request_id=state['email_request_id'], email='c@example.com')
    proposal = await scheduler.latest_pending(T, conv)
    await voice.confirm_booking(proposal_id=proposal.id)
    return scheduler, voice, free, scheduler._bookings[proposal.id]


# ── Confirm booking button ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_confirm_button_needs_email_and_current_proposal():
    scheduler, voice, _ = await voice_with_proposal()
    state = await voice.booking_state()
    assert state['stage'] == 'email_required'
    proposal_id = state['proposal']['id']
    with pytest.raises(SchedulingError, match='email'):
        await voice.confirm_booking(proposal_id=proposal_id)
    await voice.submit_email(request_id=state['email_request_id'], email='c@example.com')
    assert (await voice.booking_state())['stage'] == 'ready_to_confirm'
    with pytest.raises(SchedulingError):
        await voice.confirm_booking(proposal_id='stale-or-guessed-id')
    assert not scheduler._bookings
    result = await voice.confirm_booking(proposal_id=proposal_id)
    assert 'now confirmed' in result['message']
    assert proposal_id in scheduler._bookings
    state = await voice.booking_state()
    assert state['stage'] == 'booked' and state['booking']['id'] == proposal_id
    with pytest.raises(SchedulingError):  # nothing left to confirm
        await voice.confirm_booking(proposal_id=proposal_id)


@pytest.mark.asyncio
async def test_change_email_link_reopens_box_for_same_proposal():
    scheduler, voice, _ = await voice_with_proposal()
    state = await voice.booking_state()
    await voice.submit_email(request_id=state['email_request_id'], email='old@example.com')
    await voice.reopen_email(proposal_id=state['proposal']['id'])
    reopened = await voice.booking_state()
    assert reopened['stage'] == 'email_required'
    assert reopened['email_request_id'] != state['email_request_id']
    await voice.submit_email(request_id=reopened['email_request_id'], email='new@example.com')
    await voice.confirm_booking(proposal_id=state['proposal']['id'])
    assert scheduler._bookings[state['proposal']['id']].customer_email == 'new@example.com'


# ── Move / cancel in the scheduler ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_move_is_proposed_then_applied_and_frees_old_slot():
    scheduler, voice, free, booking = await booked_voice()
    old_start = booking.start
    booking.reminder_sent = True
    change = await scheduler.propose_move(T, 'v1', free[3].start)
    assert booking.start == old_start  # proposing changes nothing
    reply = await scheduler.apply_change(T, 'v1', change.id)
    assert reply.status == 'moved'
    assert booking.start == free[3].start
    assert booking.reminder_sent is False  # reminder follows the new time
    assert old_start in [s.start for s in await slots(scheduler)]


@pytest.mark.asyncio
async def test_move_rejects_busy_time_other_conversation_and_no_booking():
    scheduler, voice, free, booking = await booked_voice()
    other = await scheduler.propose(T, 'other', 'consultation', free[2].start, 'Someone', 'o@example.com')
    await scheduler.confirm(T, 'other', other.id)
    with pytest.raises(SchedulingError, match='unavailable'):
        await scheduler.propose_move(T, 'v1', free[2].start)
    with pytest.raises(SchedulingError, match='already at that time'):
        await scheduler.propose_move(T, 'v1', booking.start)
    with pytest.raises(SchedulingError, match='no confirmed appointment'):
        await scheduler.propose_move(T, 'stranger', free[4].start)
    change = await scheduler.propose_move(T, 'v1', free[4].start)
    with pytest.raises(SchedulingError, match='not found'):
        await scheduler.apply_change(T, 'stranger', change.id)


@pytest.mark.asyncio
async def test_cancel_applies_only_after_confirmation_and_keep_discards():
    scheduler, voice, free, booking = await booked_voice()
    change = await scheduler.propose_cancel(T, 'v1')
    await scheduler.discard_change(T, 'v1', change.id)
    assert booking.id in scheduler._bookings  # kept
    change = await scheduler.propose_cancel(T, 'v1')
    reply = await scheduler.apply_change(T, 'v1', change.id)
    assert reply.status == 'cancelled'
    assert booking.id not in scheduler._bookings
    assert booking.start in [s.start for s in await slots(scheduler)]
    assert (await voice.booking_state())['stage'] == 'cancelled'


@pytest.mark.asyncio
async def test_expired_change_cannot_apply():
    scheduler, voice, free, booking = await booked_voice()
    change = await scheduler.propose_cancel(T, 'v1')
    change.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(SchedulingError, match='expired'):
        await scheduler.apply_change(T, 'v1', change.id)
    assert booking.id in scheduler._bookings


# ── Voice: buttons and spoken replies for move/cancel ────────────────────────

@pytest.mark.asyncio
async def test_voice_move_card_then_spoken_yes_applies():
    scheduler, voice, free, booking = await booked_voice()

    async def agent_moves(_):
        await scheduler.propose_move(T, 'v1', free[5].start)
        return 'prepared'

    question = await voice._handle_turn('Can we move it to later?', agent_moves)
    assert 'Confirm move' in question and booking.start == free[0].start
    assert (await voice.booking_state())['stage'] == 'move_pending'
    assert 'Confirm move' in await voice._handle_turn('yes', never)  # vague: asks again
    done = await voice._handle_turn('Yes, confirm the move', never)
    assert 'moved' in done and booking.start == free[5].start


@pytest.mark.asyncio
async def test_voice_cancel_buttons_keep_then_cancel():
    scheduler, voice, free, booking = await booked_voice()

    async def agent_cancels(_):
        await scheduler.propose_cancel(T, 'v1')
        return 'prepared'

    question = await voice._handle_turn('Please cancel my appointment', agent_cancels)
    assert 'Cancel appointment' in question
    state = await voice.booking_state()
    assert state['stage'] == 'cancel_pending'
    await voice.discard_change(change_id=state['change']['id'])
    assert booking.id in scheduler._bookings
    await voice._handle_turn('Cancel it please', agent_cancels)
    state = await voice.booking_state()
    with pytest.raises(SchedulingError):
        await voice.confirm_change(change_id='guessed')
    result = await voice.confirm_change(change_id=state['change']['id'])
    assert 'cancelled' in result['message']
    assert booking.id not in scheduler._bookings


@pytest.mark.parametrize('text,kind,expected', [
    ('Yes, confirm the move', 'move', 'approve'),
    ('yes move it', 'move', 'approve'),
    ('Yes, cancel it', 'cancel', 'approve'),
    ('no keep it', 'cancel', 'decline'),
    ('Keep the current time', 'move', 'decline'),
    ('yes', 'move', 'unclear'),
    ('yes cancel it but', 'cancel', 'unclear'),
    ('actually make it 5pm', 'move', 'revise'),
])
def test_change_reply_rules_are_strict(text, kind, expected):
    assert classify_change_reply(text, kind) == expected


# ── Google Calendar: same event is moved or deleted ──────────────────────────

def google_app():
    return SimpleNamespace(state=SimpleNamespace(google_oauth_tokens={
        'access_token': 'fake', 'expires_at': time.time() + 3500, 'refresh_token': 'unused',
    }))


@pytest.mark.asyncio
async def test_google_move_patches_event_and_cancel_deletes_it():
    calls, busy = [], []

    def transport(request):
        calls.append(request)
        if request.url.path.endswith('/freeBusy'):
            return httpx.Response(200, json={'calendars': {'primary': {'busy': busy}}})
        if request.method == 'GET':
            return httpx.Response(404)
        if request.method == 'POST':
            body = json.loads(request.content)
            return httpx.Response(200, json={'id': body['id'], 'status': 'confirmed'})
        if request.method == 'PATCH':
            body = json.loads(request.content)
            return httpx.Response(200, json={'id': request.url.path.rsplit('/', 1)[-1],
                                             'status': 'confirmed', **body})
        if request.method == 'DELETE':
            return httpx.Response(204)
        return httpx.Response(400)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        scheduler = GoogleBackedScheduler(google_app(), google_client=client)
        day = date.today() + timedelta(days=5)
        free = await scheduler.available_slots(T, 'consultation', day)
        proposal = await scheduler.propose(T, 'g', 'consultation', free[0].start, 'C', 'c@example.com')
        await scheduler.confirm(T, 'g', proposal.id)
        # The booking's own event shows as busy in Google; it must not block moving.
        busy.append({'start': proposal.start.isoformat(), 'end': proposal.end.isoformat()})
        assert free[0].start in [s.start for s in await scheduler.available_slots(
            T, 'consultation', day, ignore_booking_id=proposal.id)]
        change = await scheduler.propose_move(T, 'g', free[2].start)
        await scheduler.apply_change(T, 'g', change.id)
        patch = [r for r in calls if r.method == 'PATCH'][-1]
        assert patch.url.params['sendUpdates'] == 'all'
        assert patch.url.path.endswith(google_event_id(proposal.id))
        assert proposal.start == free[2].start
        busy.clear()
        cancel = await scheduler.propose_cancel(T, 'g')
        await scheduler.apply_change(T, 'g', cancel.id)
        delete = [r for r in calls if r.method == 'DELETE'][-1]
        assert delete.url.params['sendUpdates'] == 'all'
        assert proposal.id not in scheduler._bookings


@pytest.mark.asyncio
async def test_google_failure_leaves_booking_unchanged():
    def transport(request):
        if request.url.path.endswith('/freeBusy'):
            return httpx.Response(200, json={'calendars': {'primary': {'busy': []}}})
        if request.method == 'GET':
            return httpx.Response(404)
        if request.method == 'POST':
            body = json.loads(request.content)
            return httpx.Response(200, json={'id': body['id'], 'status': 'confirmed'})
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        scheduler = GoogleBackedScheduler(google_app(), google_client=client)
        free = await scheduler.available_slots(T, 'consultation', date.today() + timedelta(days=5))
        proposal = await scheduler.propose(T, 'g', 'consultation', free[0].start, 'C', 'c@example.com')
        await scheduler.confirm(T, 'g', proposal.id)
        change = await scheduler.propose_move(T, 'g', free[2].start)
        with pytest.raises(SchedulingError, match='nothing was changed'):
            await scheduler.apply_change(T, 'g', change.id)
        assert proposal.start == free[0].start
        cancel = await scheduler.propose_cancel(T, 'g')
        with pytest.raises(SchedulingError):
            await scheduler.apply_change(T, 'g', cancel.id)
        assert proposal.id in scheduler._bookings


# ── Chat routes ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_booking_card_routes():
    app = FastAPI()
    app.include_router(chat_router)
    scheduler = app.state.scheduler = DemoScheduler()
    free = await slots(scheduler)
    proposal = await scheduler.propose(T, 'chat-1', 'consultation', free[0].start, 'C')
    with TestClient(app) as client:
        state = client.get('/api/v1/dev/booking-state', params={'conversation_id': 'chat-1'}).json()
        assert state['stage'] == 'email_required'
        bad = client.post('/api/v1/dev/proposal-email', json={
            'conversation_id': 'chat-1', 'proposal_id': proposal.id, 'email': 'nope'})
        assert bad.status_code == 422
        client.post('/api/v1/dev/proposal-email', json={
            'conversation_id': 'chat-1', 'proposal_id': proposal.id, 'email': 'c@example.com'})
        state = client.get('/api/v1/dev/booking-state', params={'conversation_id': 'chat-1'}).json()
        assert state['stage'] == 'ready_to_confirm'
        assert not scheduler._bookings  # saving the email never books
        client.post('/api/v1/dev/confirm', json={
            'tenant_id': T, 'conversation_id': 'chat-1', 'proposal_id': proposal.id})
        change = await scheduler.propose_cancel(T, 'chat-1')
        state = client.get('/api/v1/dev/booking-state', params={'conversation_id': 'chat-1'}).json()
        assert state['stage'] == 'cancel_pending' and state['change']['id'] == change.id
        wrong = client.post('/api/v1/dev/change/confirm', json={
            'conversation_id': 'someone-else', 'change_id': change.id})
        assert wrong.status_code == 409
        done = client.post('/api/v1/dev/change/confirm', json={
            'conversation_id': 'chat-1', 'change_id': change.id})
        assert done.json()['status'] == 'cancelled'


# ── Reminders ────────────────────────────────────────────────────────────────

def reminder_setup(sent, *, fail=False):
    def transport(request):
        if fail:
            return httpx.Response(500)
        sent.append(request)
        return httpx.Response(200, json={'id': f'msg-{len(sent)}'})
    return httpx.AsyncClient(transport=httpx.MockTransport(transport))


async def confirmed_booking(scheduler, conv, start_offset, confirmed_ago):
    """A booking starting start_offset from now, confirmed confirmed_ago before now."""
    now = datetime.now(timezone.utc)
    free = await slots(scheduler)
    proposal = await scheduler.propose(T, conv, 'consultation', free[0].start, 'Rae <b>', 'r@example.com')
    await scheduler.confirm(T, conv, proposal.id)
    proposal.start = now + start_offset
    proposal.end = proposal.start + timedelta(minutes=30)
    proposal.confirmed_at = now - confirmed_ago
    return proposal


@pytest.mark.asyncio
async def test_reminder_sent_once_when_due():
    sent = []
    scheduler = DemoScheduler()
    booking = await confirmed_booking(scheduler, 'r1', timedelta(minutes=55), timedelta(days=1))
    async with reminder_setup(sent) as client:
        worker = ReminderWorker(scheduler, api_key='k', sender='Nofa <r@zovak.tech>', client=client)
        assert await worker.run_once() == [booking.id]
        assert await worker.run_once() == []  # never twice
    body = json.loads(sent[0].content)
    assert body['to'] == ['r@example.com'] and 'Reminder' in body['subject']
    assert '&lt;b&gt;' in body['html']  # customer name is escaped
    assert sent[0].headers['Idempotency-Key'].startswith(f'reminder-{booking.id}')


@pytest.mark.asyncio
async def test_reminder_skips_not_due_late_bookings_and_cancelled():
    sent = []
    scheduler = DemoScheduler()
    later = await confirmed_booking(scheduler, 'r1', timedelta(hours=3), timedelta(days=1))
    just_booked = await confirmed_booking(scheduler, 'r2', timedelta(minutes=40), timedelta(minutes=5))
    cancelled = await confirmed_booking(scheduler, 'r3', timedelta(minutes=50), timedelta(days=1))
    change = await scheduler.propose_cancel(T, 'r3')
    cancelled.start = datetime.now(timezone.utc) + timedelta(hours=2)  # still in the future
    await scheduler.apply_change(T, 'r3', change.id)
    async with reminder_setup(sent) as client:
        worker = ReminderWorker(scheduler, api_key='k', sender='s', client=client)
        assert await worker.run_once() == []
    assert not sent
    assert later.reminder_sent is False and just_booked.reminder_sent is True


@pytest.mark.asyncio
async def test_failed_reminder_is_retried():
    scheduler = DemoScheduler()
    booking = await confirmed_booking(scheduler, 'r1', timedelta(minutes=30), timedelta(days=1))
    async with reminder_setup([], fail=True) as client:
        worker = ReminderWorker(scheduler, api_key='k', sender='s', client=client)
        assert await worker.run_once() == []
    assert booking.reminder_sent is False
    sent = []
    async with reminder_setup(sent) as client:
        worker = ReminderWorker(scheduler, api_key='k', sender='s', client=client)
        assert await worker.run_once() == [booking.id]


# ── Date context ─────────────────────────────────────────────────────────────

def test_agent_knows_today_and_never_asks_for_year():
    text = today_instructions()
    assert str(datetime.now().year) in text or str(datetime.now().year + 1) in text
    assert 'Never ask the customer for the year' in text
