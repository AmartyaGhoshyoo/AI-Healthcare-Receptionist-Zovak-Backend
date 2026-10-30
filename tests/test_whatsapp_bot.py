"""WhatsApp chat: agent replies, email typed in chat, tap-button booking card, guardrails, webhook."""
import hashlib
import hmac
import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zovak.api.routes import whatsapp as whatsapp_route
from zovak.services.mock_scheduler import DemoScheduler
from zovak.services.whatsapp_bot import WhatsAppBot

T, USER = 'demo-business', '8801712345678'
CONV = f'wa-{USER}'


class FakeCloud:
    def __init__(self):
        self.texts, self.buttons, self.read = [], [], []

    async def send_text(self, to, body):
        self.texts.append(body)

    async def send_buttons(self, to, body, buttons):
        self.buttons.append((body, buttons))

    async def mark_read(self, message_id, *, typing=False):
        self.read.append((message_id, typing))


def text(body, mid):
    return {'from': USER, 'id': mid, 'type': 'text', 'text': {'body': body}}


def tap(button_id, mid):
    return {'from': USER, 'id': mid, 'type': 'interactive',
            'interactive': {'type': 'button_reply', 'button_reply': {'id': button_id, 'title': 'x'}}}


def payload(*messages):
    return {'object': 'whatsapp_business_account', 'entry': [{'id': 'waba', 'changes': [
        {'field': 'messages', 'value': {'messages': list(messages)}}]}]}


async def setup():
    scheduler = DemoScheduler()
    free = await scheduler.available_slots(T, 'consultation', date.today() + timedelta(days=3))
    calls = []

    async def agent(sched, tenant, conversation, message, *, channel, customer_phone=None):
        calls.append((conversation, message, channel))
        if 'book' in message:
            await sched.propose(tenant, conversation, 'consultation', free[1].start, 'Amartya')
            return 'I have a slot tomorrow. What email should I use?'
        if 'move' in message:
            await sched.propose_move(tenant, conversation, free[4].start)
            return 'I can move it.'
        if 'cancel' in message:
            await sched.propose_cancel(tenant, conversation)
            return 'I can cancel it.'
        return 'How can I help with an appointment?'

    cloud = FakeCloud()
    return scheduler, free, cloud, calls, WhatsAppBot(scheduler, cloud, run_agent=agent)


@pytest.mark.asyncio
async def test_book_by_chat_type_email_and_tap_confirm():
    scheduler, free, cloud, calls, bot = await setup()
    await bot.handle_payload(payload(text('I want to book a consultation', 'm1')))
    assert calls == [(CONV, 'I want to book a consultation', 'whatsapp')]
    assert 'What email' in cloud.texts[0] and 'type the email' in cloud.texts[1]
    await bot.handle_payload(payload(text('my email is a@example.com', 'm2')))
    assert len(calls) == 1  # the email was taken directly, not sent to the agent
    body, buttons = cloud.buttons[-1]
    assert 'a@example.com' in body and buttons[0][1] == 'Confirm booking'
    assert not scheduler._bookings  # nothing booked before the tap
    await bot.handle_payload(payload(tap('book:not-the-real-id', 'm3')))
    assert 'no longer available' in cloud.texts[-1] and not scheduler._bookings
    await bot.handle_payload(payload(tap(buttons[0][0], 'm4')))
    assert "You're booked" in cloud.texts[-1] and len(scheduler._bookings) == 1
    assert cloud.read == [(m, True) for m in ('m1', 'm2', 'm3', 'm4')]  # read + "typing…"


@pytest.mark.asyncio
async def test_move_and_cancel_with_buttons_later_in_the_same_chat():
    scheduler, free, cloud, calls, bot = await setup()
    await bot.handle_payload(payload(text('book please', 'a1'), text('a@example.com', 'a2')))
    await bot.handle_payload(payload(tap(cloud.buttons[-1][1][0][0], 'a3')))
    await bot.handle_payload(payload(text('can you move it', 'a4')))
    body, buttons = cloud.buttons[-1]
    assert 'Move your' in body and [b[1] for b in buttons] == ['Confirm move', 'Keep current time']
    await bot.handle_payload(payload(tap(buttons[0][0], 'a5')))
    booking = next(iter(scheduler._bookings.values()))
    assert 'Moved to' in cloud.texts[-1] and booking.start == free[4].start
    await bot.handle_payload(payload(text('please cancel', 'a6')))
    keep = cloud.buttons[-1][1][1][0]
    await bot.handle_payload(payload(tap(keep, 'a7')))
    assert 'stays as it was' in cloud.texts[-1] and scheduler._bookings


@pytest.mark.asyncio
async def test_duplicates_media_and_rate_limit():
    scheduler, free, cloud, calls, bot = await setup()
    await bot.handle_payload(payload(text('hello', 'd1'), text('hello', 'd1')))
    assert len(calls) == 1  # Meta retried the same message
    await bot.handle_payload(payload({'from': USER, 'id': 'd2', 'type': 'image', 'image': {}}))
    assert 'only read text' in cloud.texts[-1]
    bot.max_per_hour = 3
    for i in range(5):
        await bot.handle_payload(payload(text('hi', f'r{i}')))
    assert cloud.texts.count("You've sent a lot of messages. Please wait a while, or call us to book.") == 1


@pytest.mark.asyncio
async def test_too_many_changes_in_one_chat_stops_changes():
    scheduler, free, cloud, calls, bot = await setup()
    await bot.handle_payload(payload(text('book', 'b1'), text('a@example.com', 'b2')))
    await bot.handle_payload(payload(tap(cloud.buttons[-1][1][0][0], 'b3')))
    for i in range(6):
        await bot.handle_payload(payload(text('please cancel', f'c{i}')))
        if "can't make more changes" in cloud.texts[-1]:
            break
        await bot.handle_payload(payload(tap(cloud.buttons[-1][1][1][0], f'k{i}')))  # keep it
    assert "can't make more changes" in cloud.texts[-1]
    assert await scheduler.latest_change(T, CONV) is None and scheduler._bookings


SECRET = 'app-secret'


def webhook_app():
    app = FastAPI()
    app.include_router(whatsapp_route.router)
    app.state.settings = SimpleNamespace(whatsapp_app_secret=SECRET, whatsapp_verify_token='verify-me')
    received = []

    class Bot:
        async def handle_payload(self, data):
            received.append(data)

    app.state.whatsapp = Bot()
    return app, received


def test_webhook_verification_and_signature():
    app, received = webhook_app()
    body = json.dumps(payload(text('hi', 'w1')))
    good = 'sha256=' + hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        ok = client.get('/api/v1/whatsapp/webhook', params={
            'hub.mode': 'subscribe', 'hub.verify_token': 'verify-me', 'hub.challenge': '12345'})
        assert ok.text == '12345'
        assert client.get('/api/v1/whatsapp/webhook', params={
            'hub.mode': 'subscribe', 'hub.verify_token': 'wrong', 'hub.challenge': '1'}).status_code == 403
        assert client.post('/api/v1/whatsapp/webhook', content=body,
                           headers={'x-hub-signature-256': 'sha256=bad'}).status_code == 401
        assert client.post('/api/v1/whatsapp/webhook', content=body,
                           headers={'x-hub-signature-256': good}).json() == {'ok': True}
    assert received and received[0]['entry'][0]['changes'][0]['value']['messages'][0]['id'] == 'w1'
