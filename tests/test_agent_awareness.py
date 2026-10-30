"""The agent must know about bookings changed by buttons or strict spoken replies.

Regression: a customer confirmed with the on-screen button, then asked to move
the appointment; the agent's memory still said "pending" and it refused.
"""
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zovak.agents.memory import memory_session
from zovak.agents.receptionist import make_receptionist
from zovak.api.routes.dev import router as chat_router
from zovak.services.booking_state import booking_status_for_agent
from zovak.services.mock_scheduler import DemoScheduler
from zovak.voice.sideband import VoiceSideband

T = 'demo-business'


async def notes(conversation_id):
    session = memory_session(T, conversation_id)
    try:
        return [i['content'] for i in await session.get_items() if i.get('role') == 'system']
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_button_confirm_then_move_agent_sees_confirmed_booking():
    scheduler = DemoScheduler()
    voice = VoiceSideband(session_id='s', conversation_id='v', api_key='unused',
                          scheduler=scheduler, agent_model='unused')
    free = await scheduler.available_slots(T, 'consultation', date.today() + timedelta(days=3))

    async def propose(_):
        await scheduler.propose(T, 'v', 'consultation', free[2].start, 'Amartya')
        return 'proposed'

    await voice._handle_turn('Book 11 am tomorrow', propose)
    state = await voice.booking_state()
    await voice.submit_email(request_id=state['email_request_id'], email='a@example.com')
    assert 'PENDING proposal' in await booking_status_for_agent(scheduler, T, 'v')

    await voice.confirm_booking(proposal_id=state['proposal']['id'])  # the button
    status = await booking_status_for_agent(scheduler, T, 'v')
    assert status.startswith('CONFIRMED booking') and 'PENDING' not in status
    assert any('now CONFIRMED' in n for n in await notes('v'))
    agent = make_receptionist(email_via_form=True, booking_status=status)
    assert 'CONFIRMED booking' in agent.instructions
    assert 'trust it over anything said earlier' in agent.instructions

    change = await scheduler.propose_move(T, 'v', free[4].start)
    assert 'PENDING move' in await booking_status_for_agent(scheduler, T, 'v')
    await voice.confirm_change(change_id=change.id)
    assert any('confirmed the move' in n for n in await notes('v'))
    assert 'PENDING' not in await booking_status_for_agent(scheduler, T, 'v')


@pytest.mark.asyncio
async def test_chat_card_actions_leave_notes():
    app = FastAPI()
    app.include_router(chat_router)
    scheduler = app.state.scheduler = DemoScheduler()
    free = await scheduler.available_slots(T, 'consultation', date.today() + timedelta(days=3))
    proposal = await scheduler.propose(T, 'c', 'consultation', free[0].start, 'C', 'c@example.com')
    with TestClient(app) as client:
        client.post('/api/v1/dev/confirm', json={
            'tenant_id': T, 'conversation_id': 'c', 'proposal_id': proposal.id})
        change = await scheduler.propose_cancel(T, 'c')
        client.post('/api/v1/dev/change/discard', json={'conversation_id': 'c', 'change_id': change.id})
    saved = await notes('c')
    assert any('Confirm booking' in n for n in saved)
    assert any('keep the appointment unchanged' in n for n in saved)


@pytest.mark.asyncio
async def test_status_with_nothing_booked():
    assert await booking_status_for_agent(DemoScheduler(), T, 'empty') == (
        'No appointment in this conversation yet.')
