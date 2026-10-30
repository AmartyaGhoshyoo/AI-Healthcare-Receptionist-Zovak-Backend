"""Doctor running late: only affected patients, best options, apply + notify, patient choice, staff API."""
from datetime import date, datetime, time, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zovak.api.routes.forms import router as forms_router
from zovak.api.routes.staff import router as staff_router
from zovak.services.mock_scheduler import DemoScheduler, SchedulingError
from zovak.services.recovery import RecoveryPlanner
from zovak.services.staff_auth import StaffSessions

T = 'demo-business'
DAY = date.today() + timedelta(days=3)
TZ = DemoScheduler().timezone


def at(hh, mm=0, day=DAY):
    return datetime.combine(day, time(hh, mm), TZ)


class FakeSms:
    def __init__(self):
        self.sent = []

    async def send(self, to, body):
        self.sent.append((to, body))


async def book(s, conv, hh, mm=0, doctor='dr-rahman', service='consultation'):
    p = await s.propose(T, conv, service, at(hh, mm), conv.title(), f'{conv}@example.com',
                        doctor_id=doctor, customer_phone=f'+88017000{len(s._bookings):05d}')
    await s.confirm(T, conv, p.id)
    return p


@pytest.mark.asyncio
async def test_only_patients_the_delay_reaches_are_affected():
    s = DemoScheduler()
    ali, bina, chandni, dipu = [await book(s, n, h, m) for n, h, m in
                                [('ali', 10, 0), ('bina', 10, 30), ('chandni', 11, 0), ('dipu', 14, 0)]]
    plan = await RecoveryPlanner(s).preview('dr-rahman', 60, 'Dr. Rahman', now=at(9, 30))
    assert [i.patient for i in plan.items] == ['Ali', 'Bina', 'Chandni']  # Dipu untouched
    assert [i.shifted_start for i in plan.items] == [at(10, 30), at(11), at(11, 30)]
    first = plan.items[0]
    kinds = [o.kind for o in first.options]
    assert kinds[0] == 'other_doctor' and 'Chowdhury' in first.options[0].label  # no-wait option first
    assert kinds[1] == 'shifted' and kinds[-1] == 'cancel' and 'next_slot' in kinds
    assert first.default_option == 'shifted'
    # Other times with the late doctor only start once she has arrived (10:30).
    assert all(o.start >= at(10, 30) for i in plan.items for o in i.options if o.kind == 'next_slot')
    assert not any(b.start != orig for b, orig in [(ali, at(10)), (dipu, at(14))])  # preview changes nothing


@pytest.mark.asyncio
async def test_shifts_skip_lunch_and_past_closing_get_a_next_slot():
    s = DemoScheduler()
    await book(s, 'eshita', 12, 30)
    await book(s, 'fahim', 16, 30)
    lunch = await RecoveryPlanner(s).preview('dr-rahman', 45, 'x', now=at(12, 15))
    assert lunch.items[0].shifted_start == at(14)  # 13:00 would hit lunch (1–2 PM)
    late = await RecoveryPlanner(s).preview('dr-rahman', 60, 'x', now=at(16, 15))
    item = late.items[-1]
    assert item.shifted_start is None and item.default_option.startswith('slot')  # Rahman closes at 5
    assert any(o.start and o.start.date() == DAY + timedelta(days=1) for o in item.options)


@pytest.mark.asyncio
async def test_send_applies_shifts_then_patients_choose_once():
    s, sms = DemoScheduler(), FakeSms()
    for n, h, m in [('ali', 10, 0), ('bina', 10, 30), ('chandni', 11, 0)]:
        await book(s, n, h, m)
    planner = RecoveryPlanner(s, sms=sms, public_base_url='https://demo.ngrok.app')
    plan = await planner.preview('dr-rahman', 60, 'Dr. Rahman', now=at(9, 30))
    await planner.send(plan.id)
    starts = sorted(b.start for b in s._bookings.values())
    assert starts == [at(10, 30), at(11), at(11, 30)]  # shifted latest-first without clashes
    assert len(sms.sent) == 3 and 'https://demo.ngrok.app/update/' in sms.sent[0][1]
    assert 'running about 60 minutes late' in sms.sent[0][1]
    ali = plan.items[0]
    await planner.choose(ali.token, 'other')  # Dr. Chowdhury at the original 10:00
    booking = s._bookings[ali.booking_id]
    assert booking.doctor_id == 'dr-chowdhury' and booking.start == at(10)
    with pytest.raises(SchedulingError, match='already chose'):
        await planner.choose(ali.token, 'cancel')
    await planner.choose(plan.items[1].token, 'shifted')
    await planner.choose(plan.items[2].token, 'cancel')
    statuses = [i['status'] for i in planner.view(plan)['items']]
    assert statuses == ['rescheduled', 'kept', 'cancelled']
    assert len(s._bookings) == 2
    with pytest.raises(SchedulingError, match='already sent'):
        await planner.send(plan.id)


@pytest.fixture
def portal():
    app = FastAPI()
    app.include_router(staff_router)
    app.include_router(forms_router)
    s = DemoScheduler()
    app.state.scheduler = s
    app.state.staff_sessions = StaffSessions()
    app.state.recovery = RecoveryPlanner(s, sms=FakeSms())
    return app, s


def signin(client, staff_id, pin):
    r = client.post('/api/v1/staff/login', json={'staff_id': staff_id, 'pin': pin})
    return r, {'x-staff-token': r.json().get('token', '')}


@pytest.mark.asyncio
async def test_staff_portal_roles_and_delay_flow(portal):
    app, s = portal
    today = datetime.now(TZ).date()
    for doctor, hour in [('dr-rahman', 16), ('dr-karim', 18)]:
        p = await s.propose(T, f'c-{doctor}', s.doctors[doctor].services[0], datetime.combine(today + timedelta(days=1), time(hour), TZ),
                            'Pat', 'p@example.com', doctor_id=doctor, customer_phone='+8801700000999')
        await s.confirm(T, f'c-{doctor}', p.id)
    with TestClient(app) as client:
        assert 'pin' not in client.get('/api/v1/staff/directory').json()[0]
        bad, _ = signin(client, 'staff-rahman', '0000')
        assert bad.status_code == 401
        ok, doc = signin(client, 'staff-rahman', '1111')
        assert ok.json()['staff']['role'] == 'doctor'
        tomorrow = (today + timedelta(days=1)).isoformat()
        sched = client.get('/api/v1/staff/schedule', params={'day': tomorrow}, headers=doc).json()
        assert [d['id'] for d in sched['doctors']] == ['dr-rahman'] and len(sched['doctors'][0]['bookings']) == 1
        _, boss = signin(client, 'staff-manager', '9999')
        everyone = client.get('/api/v1/staff/schedule', params={'day': tomorrow}, headers=boss).json()
        assert len(everyone['doctors']) == 4
        # A doctor's delay always applies to their own schedule.
        preview = client.post('/api/v1/staff/delay/preview', json={'doctor_id': 'dr-karim', 'minutes': 30},
                              headers=doc).json()
        assert preview['doctor_id'] == 'dr-rahman' and preview['status'] == 'draft'
        assert client.get('/api/v1/staff/schedule', headers={'x-staff-token': 'nope'}).status_code == 401


@pytest.mark.asyncio
async def test_patient_update_page(portal):
    app, s = portal
    for n, h, m in [('ali', 10, 0), ('bina', 10, 30)]:
        await book(s, n, h, m)
    planner = app.state.recovery
    plan = await planner.preview('dr-rahman', 60, 'x', now=at(9, 30))
    await planner.send(plan.id)
    token = plan.items[0].token
    with TestClient(app) as client:
        page = client.get(f'/update/{token}').text
        assert 'running about <b>60 minutes</b> late' in page and 'Dr. Imran Chowdhury' in page
        done = client.post(f'/update/{token}', content='option=cancel',
                           headers={'content-type': 'application/x-www-form-urlencoded'}).text
        assert 'You chose' in done and 'Cancel my appointment' in done
        assert client.get('/update/not-a-token').status_code == 404


@pytest.mark.asyncio
async def test_any_day_shows_in_the_doctors_login(portal):
    """A booking made through chat/voice/phone appears in that doctor's schedule on its day."""
    app, s = portal
    later = datetime.now(TZ).date() + timedelta(days=6)
    p = await s.propose(T, 'web-chat', 'consultation', datetime.combine(later, time(10), TZ), 'Mitu',
                        'm@example.com', doctor_id='dr-chowdhury')
    await s.confirm(T, 'web-chat', p.id)
    with TestClient(app) as client:
        _, chowdhury = signin(client, 'staff-chowdhury', '2222')
        sched = client.get('/api/v1/staff/schedule', params={'day': later.isoformat()}, headers=chowdhury).json()
        assert [b['patient'] for b in sched['doctors'][0]['bookings']] == ['Mitu']
        _, rahman = signin(client, 'staff-rahman', '1111')
        other = client.get('/api/v1/staff/schedule', params={'day': later.isoformat()}, headers=rahman).json()
        assert other['doctors'][0]['bookings'] == []  # other doctors don't see it


# ── When the delay counts from ──────────────────────────────────────────────

async def karim_day(s):
    for n, h, m in [('chandni', 11, 30), ('dipu', 12, 0), ('eshita', 12, 30), ('amartya', 13, 0)]:
        await book(s, n, h, m, doctor='dr-karim', service='skin_consultation')


@pytest.mark.asyncio
async def test_evening_report_applies_to_the_next_working_day():
    s = DemoScheduler()
    await book(s, 'ali', 9, 0)
    await book(s, 'bina', 9, 30)
    plan = await RecoveryPlanner(s).preview('dr-rahman', 60, 'x', now=at(21, 0, day=DAY - timedelta(days=1)))
    assert plan.starts_at == at(9)  # her day was over: counted from tomorrow's 9:00
    assert [i.shifted_start for i in plan.items] == [at(10), at(10, 30)]


@pytest.mark.asyncio
async def test_before_the_shift_counts_from_the_start_time():
    s = DemoScheduler()
    await book(s, 'ali', 9, 0)
    plan = await RecoveryPlanner(s).preview('dr-rahman', 30, 'x', now=at(7, 0))
    assert [i.shifted_start for i in plan.items] == [at(9, 30)]  # not absorbed by 7:00 → 9:00


@pytest.mark.asyncio
async def test_late_for_a_named_time_moves_only_from_there():
    s = DemoScheduler()
    await karim_day(s)
    plan = await RecoveryPlanner(s).preview('dr-karim', 30, 'x', now=at(7, 0), late_from=at(13, 0))
    assert [(i.patient, i.shifted_start) for i in plan.items] == [('Amartya', at(13, 30))]


@pytest.mark.asyncio
async def test_nobody_affected_says_why_in_plain_words():
    s = DemoScheduler()
    await karim_day(s)
    planner = RecoveryPlanner(s)
    plan = await planner.preview('dr-karim', 30, 'x', now=at(7, 0))  # Karim starts at 11:00
    assert plan.items == [] and 'Chandni at 11:30' in plan.note and 'ends at 11:30' in plan.note
    empty = await planner.preview('dr-sultana', 30, 'x', now=at(7, 0))
    assert 'no patients' in empty.note
    with pytest.raises(SchedulingError, match='already passed'):
        await planner.preview('dr-karim', 30, 'x', now=at(15, 0), late_from=at(9, 0))


@pytest.mark.asyncio
async def test_portal_accepts_a_late_from_time(portal):
    app, s = portal
    later = datetime.now(TZ).date() + timedelta(days=2)
    for n, h, m in [('a', 13, 0), ('b', 13, 30), ('c', 14, 30)]:
        p = await s.propose(T, n, 'skin_consultation', datetime.combine(later, time(h, m), TZ), n,
                            doctor_id='dr-karim', customer_phone='+8801700000001')
        await s.confirm(T, n, p.id)
    with TestClient(app) as client:
        _, karim = signin(client, 'staff-karim', '4444')
        plan = client.post('/api/v1/staff/delay/preview', headers=karim, json={
            'minutes': 30, 'late_from': f'{later.isoformat()}T13:00'}).json()
        assert [i['patient'] for i in plan['items']] == ['a', 'b'] and plan['starts_at'].endswith('13:00:00+06:00')


def test_staff_assistant_builds_with_its_delay_rules():
    from zovak.agents.staff import make_staff_agent
    from zovak.services.clinic import STAFF
    agent = make_staff_agent(STAFF['staff-karim'])
    assert 'late_from' in agent.instructions and 'never reuse an old result' in agent.instructions
    assert {t.name for t in agent.tools} == {'schedule', 'preview_delay', 'send_delay_plan'}


def test_each_sign_in_gets_a_fresh_assistant_memory(portal, monkeypatch):
    import zovak.agents.staff as staff_agent
    app, _ = portal
    app.state.settings = type('S', (), {'openai_api_key': 'test'})()
    used = []

    async def fake_agent(member, scheduler, planner, message, *, chat_id):
        used.append(chat_id)
        return 'ok'

    monkeypatch.setattr(staff_agent, 'run_staff_agent', fake_agent)
    with TestClient(app) as client:
        _, first = signin(client, 'staff-karim', '4444')
        client.post('/api/v1/staff/chat', json={'message': 'hi'}, headers=first)
        client.post('/api/v1/staff/chat', json={'message': 'again'}, headers=first)
        _, second = signin(client, 'staff-karim', '4444')
        client.post('/api/v1/staff/chat', json={'message': 'hi'}, headers=second)
    assert used[0] == used[1] != used[2]  # same sign-in keeps its chat; a new one starts fresh
    assert first['x-staff-token'] not in used[0]  # the token never becomes a memory key
