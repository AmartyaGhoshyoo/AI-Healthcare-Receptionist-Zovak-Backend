"""Doctors: per-doctor availability, hours and lunch, cards, Google sharing, demo data."""
import json
import time as clock
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import httpx
import pytest

from zovak.services.booking_state import booking_state, booking_status_for_agent
from zovak.services.clinic import DOCTORS, STAFF, seed_demo_bookings
from zovak.services.google_scheduler import GoogleBackedScheduler
from zovak.services.mock_scheduler import DemoScheduler, SchedulingError

T = 'demo-business'
DAY = date.today() + timedelta(days=3)


def at(hh, mm=0):
    return datetime.combine(DAY, time(hh, mm), DemoScheduler().timezone)


@pytest.mark.asyncio
async def test_each_doctor_has_own_hours_lunch_and_bookings():
    s = DemoScheduler()
    rahman = [x.start for x in await s.available_slots(T, 'consultation', DAY, doctor_id='dr-rahman')]
    assert at(9) in rahman and at(13) not in rahman and at(17) not in rahman  # lunch 13–14, ends 17
    chowdhury = [x.start for x in await s.available_slots(T, 'consultation', DAY, doctor_id='dr-chowdhury')]
    assert at(9) not in chowdhury and at(17) in chowdhury  # starts 10, ends 18
    # Two GPs can see two patients at 11:00; a third one at 11:00 is impossible.
    a = await s.propose(T, 'a', 'consultation', at(11), 'A', 'a@example.com', doctor_id='dr-rahman')
    await s.confirm(T, 'a', a.id)
    b = await s.propose(T, 'b', 'consultation', at(11), 'B', 'b@example.com')
    assert b.doctor_id == 'dr-chowdhury'  # auto-assigned to the free GP
    await s.confirm(T, 'b', b.id)
    with pytest.raises(SchedulingError):
        await s.propose(T, 'c', 'consultation', at(11), 'C')


@pytest.mark.asyncio
async def test_doctor_must_offer_the_service():
    s = DemoScheduler()
    with pytest.raises(SchedulingError, match="doesn't offer"):
        await s.available_slots(T, 'skin_consultation', DAY, doctor_id='dr-sultana')
    karim = await s.available_slots(T, 'skin_consultation', DAY)
    assert {x.doctor_id for x in karim} == {'dr-karim'}


@pytest.mark.asyncio
async def test_offered_doctors_become_cards_until_a_proposal_exists():
    s = DemoScheduler()
    s.offer_doctors(T, 'c', ['dr-rahman', 'dr-chowdhury', 'not-a-doctor'])
    state = await booking_state(s, T, 'c', proposal=None, email_ready=False, email_request_id=None)
    names = [d['name'] for d in state['doctor_options']]
    assert names == ['Dr. Ayesha Rahman', 'Dr. Imran Chowdhury']
    assert state['doctor_options'][0]['services'] == ['General consultation', 'Follow-up visit']
    assert state['doctor_options'][0]['next_free']
    p = await s.propose(T, 'c', 'consultation', at(9), 'P', doctor_id='dr-rahman')
    state = await booking_state(s, T, 'c', proposal=p, email_ready=False, email_request_id=None)
    assert state['doctor_options'] == [] and state['proposal']['doctor_name'] == 'Dr. Ayesha Rahman'
    assert 'with Dr. Ayesha Rahman' in await booking_status_for_agent(s, T, 'c')


@pytest.mark.asyncio
async def test_google_busy_from_our_own_bookings_only_blocks_that_doctor():
    app = SimpleNamespace(state=SimpleNamespace(google_oauth_tokens={
        'access_token': 'fake', 'expires_at': clock.time() + 3500, 'refresh_token': 'x'}))
    busy = []

    def transport(request):
        if request.url.path.endswith('/freeBusy'):
            return httpx.Response(200, json={'calendars': {'primary': {'busy': busy}}})
        if request.method == 'GET':
            return httpx.Response(404)
        body = json.loads(request.content)
        return httpx.Response(200, json={'id': body['id'], 'status': 'confirmed'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        s = GoogleBackedScheduler(app, google_client=client)
        a = await s.propose(T, 'a', 'consultation', at(11), 'A', 'a@example.com', doctor_id='dr-rahman')
        await s.confirm(T, 'a', a.id)
        # Google reports the event we created as busy (merged with a back-to-back one).
        busy.append({'start': at(11).isoformat(), 'end': at(11, 30).isoformat()})
        gp = await s.available_slots(T, 'consultation', DAY, doctor_id='dr-chowdhury')
        assert at(11) in [x.start for x in gp]  # Chowdhury is still free at 11
        busy.append({'start': at(15).isoformat(), 'end': at(16).isoformat()})  # an outside event
        gp = await s.available_slots(T, 'consultation', DAY, doctor_id='dr-chowdhury')
        assert at(15) not in [x.start for x in gp]  # outside events block everyone


def test_demo_data_and_staff():
    s = DemoScheduler()
    count = seed_demo_bookings(s)
    assert count == len(s._bookings) > 10
    assert all(b.source == 'demo' and b.doctor_id in DOCTORS and b.customer_phone for b in s._bookings.values())
    assert {m.role for m in STAFF.values()} == {'doctor', 'receptionist', 'manager'}
    assert all(m.doctor_id in DOCTORS for m in STAFF.values() if m.role == 'doctor')


@pytest.mark.asyncio
async def test_form_keeps_the_same_doctor():
    from zovak.services.booking_forms import BookingForms
    s = DemoScheduler()
    forms = BookingForms(s)
    p = await s.propose(T, 'f', 'consultation', at(10), 'F', doctor_id='dr-chowdhury',
                        customer_phone='+8801700000009')
    form = await forms.create(T, p, '+8801700000009')
    assert {x.doctor_id for x in await forms.slots(form, DAY)} == {'dr-chowdhury'}
    done = await forms.submit(form.token, action='confirm', name='F', email='f@example.com',
                              start_iso=at(16).isoformat())
    booking = s._bookings[done.booking_id]
    assert booking.doctor_id == 'dr-chowdhury' and booking.customer_phone == '+8801700000009'


@pytest.mark.asyncio
async def test_demo_data_is_around_now_so_a_delay_affects_someone():
    from zovak.services.recovery import RecoveryPlanner
    s = DemoScheduler()
    morning = datetime.combine(DAY, time(9, 40), s.timezone)
    seed_demo_bookings(s, now=morning)
    plan = await RecoveryPlanner(s).preview('dr-rahman', 45, 'x', now=morning)
    assert 2 <= len(plan.items) <= 5  # a run of upcoming visits; the gap stops the ripple
    evening = datetime.combine(DAY, time(18, 30), s.timezone)
    s2 = DemoScheduler()
    seed_demo_bookings(s2, now=evening)
    assert all(b.start > evening for b in s2._bookings.values())  # day over: demo starts tomorrow


# ── Picking a doctor replaces the cards with a "chosen" chip ────────────────

from zovak.services.doctor_choice import pick_offered_doctor  # noqa: E402

GPS = ['dr-rahman', 'dr-chowdhury']


async def _state(s, conv='c'):
    return await booking_state(s, T, conv, proposal=None, email_ready=False, email_request_id=None)


@pytest.mark.asyncio
@pytest.mark.parametrize('message, picked', [
    ("I'd like to book with Dr. Ayesha Rahman (General Practice).", 'dr-rahman'),  # the Choose button
    ('chowdhury please', 'dr-chowdhury'),  # typed
    ('Dr. Chowdry, tomorrow morning', 'dr-chowdhury'),  # speech-to-text spelling
    ('Rahman or Chowdhury, whoever is free', None),  # ambiguous: Nofa asks
    ('Not Rahman', None),  # turning one down is not a pick
    ('What time is it free?', None),
])
async def test_picking_an_offered_doctor(message, picked):
    s = DemoScheduler()
    s.offer_doctors(T, 'c', GPS)
    assert pick_offered_doctor(s, T, 'c', message) == picked
    state = await _state(s)
    if picked:
        assert state['doctor_options'] == [] and state['chosen_doctor']['id'] == picked
    else:
        assert len(state['doctor_options']) == 2 and state['chosen_doctor'] is None


@pytest.mark.asyncio
async def test_chosen_chip_lifecycle():
    s = DemoScheduler()
    s.offer_doctors(T, 'c', GPS)
    pick_offered_doctor(s, T, 'c', 'Rahman')
    s.offer_doctors(T, 'c', ['dr-karim'])  # asked for another kind of care: cards again
    state = await _state(s)
    assert [d['id'] for d in state['doctor_options']] == ['dr-karim'] and state['chosen_doctor'] is None
    pick_offered_doctor(s, T, 'c', 'Karim')
    p = await s.propose(T, 'c', 'skin_consultation', at(12), 'P', doctor_id='dr-karim')
    state = await booking_state(s, T, 'c', proposal=p, email_ready=False, email_request_id=None)
    assert state['chosen_doctor'] is None  # the booking card shows the doctor from here on
    await s.confirm(T, 'c', p.id)
    s.offer_doctors(T, 'c', GPS)  # a second appointment
    state = await _state(s)
    assert state['stage'] == 'booked' and state['booking'] and len(state['doctor_options']) == 2


@pytest.mark.asyncio
async def test_voice_turn_picks_the_doctor_before_the_agent_runs():
    from zovak.voice.sideband import VoiceSideband
    s = DemoScheduler()
    s.offer_doctors(T, 'call', GPS)
    voice = VoiceSideband(session_id='live', conversation_id='call', api_key='unused', scheduler=s,
                          agent_model='unused')

    async def agent(_):
        return 'What day works for you?'

    await voice._handle_turn('Dr. Rahman please', agent)
    assert s.offered_doctors(T, 'call') == [] and s.chosen_doctor(T, 'call') == 'dr-rahman'


def test_never_offers_every_doctor_at_once():
    s = DemoScheduler()
    with pytest.raises(SchedulingError, match='one specialty'):
        s.offer_doctors(T, 'c', list(DOCTORS))
    assert s.offered_doctors(T, 'c') == []
    assert s.offer_doctors(T, 'c', GPS) == GPS


# ── Time buttons for the chosen doctor ───────────────────────────────────────

from zovak.services.booking_state import free_days, slot_request  # noqa: E402


@pytest.mark.asyncio
async def test_chosen_doctor_comes_with_free_time_buttons():
    s = DemoScheduler()
    taken = await s.propose(T, 'other', 'consultation', at(10), 'X', doctor_id='dr-rahman')  # held by someone else
    s.offer_doctors(T, 'c', GPS)
    pick_offered_doctor(s, T, 'c', 'Rahman please')
    state = await _state(s)
    days = state['chosen_doctor']['slots']
    assert 1 <= len(days) <= 3 and all(d['times'] for d in days)
    later = next(d for d in await free_days(s, T, 'c', 'dr-rahman', days=5) if d['date'] == DAY.isoformat())
    times = [datetime.fromisoformat(t) for t in later['times']]
    assert at(9) in times and at(13) not in times  # lunch is never a button
    assert taken.start not in times  # another patient's hold isn't offered


def test_tapped_time_reads_like_the_patient_saying_it():
    moment = datetime(2026, 10, 30, 9, 30, tzinfo=DemoScheduler().timezone)
    assert slot_request(moment, 'Dr. Ayesha Rahman') == "I'd like Friday, October 30 at 9:30 AM with Dr. Ayesha Rahman."


@pytest.mark.asyncio
async def test_voice_time_button_is_handled_like_speech_and_only_proposes():
    from zovak.voice.sideband import VoiceSideband
    s = DemoScheduler()
    voice = VoiceSideband(session_id='live', conversation_id='call', api_key='unused', scheduler=s, agent_model='unused')
    with pytest.raises(SchedulingError, match='Choose a doctor'):
        await voice.pick_slot(start=at(9).isoformat())
    s.offer_doctors(T, 'call', GPS)
    pick_offered_doctor(s, T, 'call', 'Chowdhury')
    heard = []

    async def agent(text):
        heard.append(text)
        return 'What name should I put it under?'

    first = (await free_days(s, T, 'call', 'dr-chowdhury'))[0]['times'][0]
    reply = await voice.pick_slot(start=first, run_agent=agent)
    assert reply['message'] == 'What name should I put it under?'
    assert heard[0].startswith("I'd like ") and heard[0].endswith('with Dr. Imran Chowdhury.')
    assert not s._bookings  # a tap never books by itself
    with pytest.raises(SchedulingError, match='just taken'):
        await voice.pick_slot(start=at(14).isoformat(), run_agent=agent)  # his lunch: never a button


@pytest.mark.asyncio
async def test_a_tapped_time_collapses_the_buttons_until_changed():
    from zovak.services.doctor_choice import pick_tapped_time
    s = DemoScheduler()
    s.offer_doctors(T, 'c', GPS)
    pick_offered_doctor(s, T, 'c', 'Rahman')
    # A day with a few free times (late in the afternoon, today may have only one left).
    first = next(d for d in (await _state(s))['chosen_doctor']['slots'] if len(d['times']) > 2)['times'][1]
    tapped = slot_request(datetime.fromisoformat(first), 'Dr. Ayesha Rahman')
    assert await pick_tapped_time(s, T, 'c', tapped) == datetime.fromisoformat(first)
    state = await _state(s)
    assert state['chosen_doctor']['picked'] == first and state['chosen_doctor']['slots']  # list kept for "Change"
    lunch = datetime.combine(datetime.fromisoformat(first).date(), time(13), s.timezone)
    assert await pick_tapped_time(s, T, 'c', slot_request(lunch, 'Dr. Ayesha Rahman')) is None  # lunch: not free
    assert s.picked_time(T, 'c') == datetime.fromisoformat(first)  # the earlier pick stays
    s.offer_doctors(T, 'c', ['dr-karim'])  # a different kind of doctor: start over
    assert s.picked_time(T, 'c') is None
    pick_offered_doctor(s, T, 'c', 'Karim')
    p = await s.propose(T, 'c', 'skin_consultation', at(12), 'P', doctor_id='dr-karim')
    assert s.picked_time(T, 'c') is None and (await booking_state(
        s, T, 'c', proposal=p, email_ready=False, email_request_id=None))['chosen_doctor'] is None


# ── Your flow: name a doctor directly, pick a time, change your mind ────────

@pytest.mark.asyncio
async def test_naming_a_doctor_directly_shows_their_time_buttons():
    from zovak.services.doctor_choice import read_choices
    s = DemoScheduler()  # no cards were offered first
    await read_choices(s, T, 'c', 'i would like to book an appointment with dr farhan karim for skin rash')
    state = await _state(s)
    assert state['chosen_doctor']['id'] == 'dr-karim' and state['chosen_doctor']['slots']
    assert 'Dr. Farhan Karim is chosen; their free times are showing' in await booking_status_for_agent(s, T, 'c')
    s.offer_doctors(T, 'c', ['dr-karim'])  # Nofa then "offers" the same doctor: the choice stays
    state = await _state(s)
    assert state['doctor_options'] == [] and state['chosen_doctor']['id'] == 'dr-karim'


@pytest.mark.asyncio
@pytest.mark.parametrize('message', ['what time are available', 'show me all', 'I have a rash', 'I need a doctor'])
async def test_messages_without_a_name_choose_nobody(message):
    from zovak.services.doctor_choice import read_choices
    s = DemoScheduler()
    await read_choices(s, T, 'c', message)
    assert s.chosen_doctor(T, 'c') is None


@pytest.mark.asyncio
async def test_change_time_or_doctor_brings_the_choice_back():
    from zovak.services.doctor_choice import read_choices
    s = DemoScheduler()
    await read_choices(s, T, 'c', 'Dr. Ayesha Rahman please')
    first = (await _state(s))['chosen_doctor']['slots'][0]['times'][0]
    await read_choices(s, T, 'c', slot_request(datetime.fromisoformat(first), 'Dr. Ayesha Rahman'))
    assert (await _state(s))['chosen_doctor']['picked'] == first
    assert 'the patient picked' in await booking_status_for_agent(s, T, 'c')
    await read_choices(s, T, 'c', 'Actually can I change the time?')
    state = await _state(s)
    assert state['chosen_doctor']['picked'] is None and state['chosen_doctor']['slots']  # buttons back
    await read_choices(s, T, 'c', 'I want a different doctor')
    assert s.chosen_doctor(T, 'c') is None  # doctor choice starts again


# ── Changing the time after the booking card is ready ────────────────────────

@pytest.mark.asyncio
async def test_change_time_while_review_card_shows_brings_buttons_back_and_keeps_email():
    from zovak.services.doctor_choice import read_choices
    s = DemoScheduler()
    p = await s.propose(T, 'c', 'skin_consultation', at(16, 30), 'Amartya Ghosh', 'amartya@example.com',
                        doctor_id='dr-karim')
    await read_choices(s, T, 'c', 'can I change the time')
    state = await _state(s)
    assert await s.latest_pending(T, 'c') is None and p.id not in s._proposals  # set aside
    assert state['chosen_doctor']['id'] == 'dr-karim' and state['chosen_doctor']['slots']  # buttons back
    assert 'already known' in await booking_status_for_agent(s, T, 'c')
    again = await s.propose(T, 'c', 'skin_consultation', at(17), 'Amartya Ghosh', doctor_id='dr-karim')
    assert again.customer_email == 'amartya@example.com'  # never asked twice
    ready = await booking_state(s, T, 'c', proposal=again, email_ready=bool(again.customer_email),
                                email_request_id=again.id)
    assert ready['stage'] == 'ready_to_confirm'


@pytest.mark.asyncio
async def test_typing_a_new_time_keeps_the_email_too():
    s = DemoScheduler()
    await s.propose(T, 'c', 'skin_consultation', at(16, 30), 'A', 'a@example.com', doctor_id='dr-karim')
    newer = await s.propose(T, 'c', 'skin_consultation', at(17), 'A', doctor_id='dr-karim')  # "5 pm"
    assert newer.customer_email == 'a@example.com' and len(s._proposals) == 1


@pytest.mark.asyncio
async def test_call_reuses_email_typed_on_screen_for_a_new_time():
    from zovak.voice.sideband import VoiceSideband
    s = DemoScheduler()
    voice = VoiceSideband(session_id='live', conversation_id='call', api_key='unused', scheduler=s, agent_model='unused')

    async def book_430(_):
        await s.propose(T, 'call', 'skin_consultation', at(16, 30), 'A', doctor_id='dr-karim')
        return 'proposed'

    await voice._handle_turn('Dr. Karim at 4:30, I am A', book_430)
    state = await voice.booking_state()
    await voice.submit_email(request_id=state['email_request_id'], email='a@example.com')

    async def book_5(_):
        await s.propose(T, 'call', 'skin_consultation', at(17), 'A', doctor_id='dr-karim')
        return 'proposed'

    reply = await voice._handle_turn('Can I change the time to 5 pm?', book_5)
    assert 'email you entered earlier' in reply
    assert (await voice.booking_state())['stage'] == 'ready_to_confirm'  # no email box again


@pytest.mark.asyncio
@pytest.mark.parametrize('said, reopens', [
    ('can i chane the time', True), ('could I switch to another slot', True), ('move it to a later time', True),
    ('can you check the time', False), ('what time is it booked for', False), ('5 pm', False),
])
async def test_change_time_wording(said, reopens):
    from zovak.services.doctor_choice import read_choices
    s = DemoScheduler()
    await s.propose(T, 'c', 'consultation', at(10), 'A', 'a@example.com', doctor_id='dr-rahman')
    await read_choices(s, T, 'c', said)
    assert (await s.latest_pending(T, 'c') is None) == reopens


@pytest.mark.asyncio
async def test_email_is_remembered_even_after_the_proposal_expires():
    s = DemoScheduler()
    p = await s.propose(T, 'c', 'skin_consultation', at(16, 30), 'A', doctor_id='dr-karim')
    await s.set_proposal_email(T, 'c', p.id, 'a@example.com')  # typed in the email box
    p.status = 'expired'  # the patient took a while
    later = await s.propose(T, 'c', 'skin_consultation', at(17), 'A', doctor_id='dr-karim')
    assert later.customer_email == 'a@example.com'


@pytest.mark.asyncio
async def test_change_email_really_asks_again():
    s = DemoScheduler()
    p = await s.propose(T, 'c', 'consultation', at(10), 'A', 'old@example.com', doctor_id='dr-rahman')
    await s.clear_proposal_email(T, 'c', p.id)  # "Change email"
    again = await s.propose(T, 'c', 'consultation', at(11), 'A', doctor_id='dr-rahman')
    assert again.customer_email is None


@pytest.mark.asyncio
async def test_screen_notes_only_on_the_website():
    s = DemoScheduler()
    s.choose_doctor(T, 'c', 'dr-rahman')
    assert 'WEBSITE SCREEN' in await booking_status_for_agent(s, T, 'c', 'chat')
    assert 'WEBSITE SCREEN' in await booking_status_for_agent(s, T, 'c', 'web')
    for channel in ('phone', 'whatsapp'):
        assert 'WEBSITE SCREEN' not in await booking_status_for_agent(s, T, 'c', channel)


@pytest.mark.asyncio
async def test_a_note_instead_of_an_email_is_never_stored():
    s = DemoScheduler()
    await s.propose(T, 'c', 'consultation', at(10), 'A', 'a@example.com', doctor_id='dr-rahman')
    again = await s.propose(T, 'c', 'consultation', at(11), 'A', 'already entered on screen', doctor_id='dr-rahman')
    assert again.customer_email == 'a@example.com'
    await s.discard(T, 'c', again.id)  # e.g. set aside to pick another time
    status = await booking_status_for_agent(s, T, 'c')
    assert 'already known: A' in status and 'WITHOUT customer_email' in status


@pytest.mark.asyncio
async def test_google_calendar_scheduler_also_keeps_the_typed_email():
    """The real (Google-backed) scheduler, not just the demo one: this is what the website uses."""
    app = SimpleNamespace(state=SimpleNamespace(google_oauth_tokens={
        'access_token': 'fake', 'expires_at': clock.time() + 3500, 'refresh_token': 'x'}))

    def transport(request):
        return httpx.Response(200, json={'calendars': {'primary': {'busy': []}}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        s = GoogleBackedScheduler(app, google_client=client)
        p = await s.propose(T, 'c', 'skin_consultation', at(17), 'Amartya Ghosh', doctor_id='dr-karim')
        await s.set_proposal_email(T, 'c', p.id, 'amartya@example.com')  # typed in the email box
        assert await s.reopen_times(T, 'c')  # "I would like to change the time"
        again = await s.propose(T, 'c', 'skin_consultation', at(16, 30), 'Amartya Ghosh', None, doctor_id='dr-karim')
        assert again.customer_email == 'amartya@example.com'
        noted = await s.propose(T, 'c', 'skin_consultation', at(17, 30), 'Amartya Ghosh', 'already entered',
                                doctor_id='dr-karim')
        assert noted.customer_email == 'amartya@example.com'  # a note is never stored as the email


@pytest.mark.asyncio
@pytest.mark.parametrize('said', ['i would like to change the mail', 'my email is wrong', 'can I use a new e-mail',
                                  'chane my email please'])
async def test_change_email_in_chat_reopens_the_email_box(said):
    from zovak.services.doctor_choice import read_choices
    s = DemoScheduler()
    p = await s.propose(T, 'c', 'consultation', at(10), 'A', 'a@example.com', doctor_id='dr-rahman')
    assert await read_choices(s, T, 'c', said) == 'email'
    assert p.customer_email is None and p.id in s._proposals  # same appointment, email box again
    state = await booking_state(s, T, 'c', proposal=p, email_ready=False, email_request_id=p.id)
    assert state['stage'] == 'email_required'


@pytest.mark.asyncio
async def test_email_words_that_are_not_a_change_leave_it_alone():
    from zovak.services.doctor_choice import read_choices
    s = DemoScheduler()
    p = await s.propose(T, 'c', 'consultation', at(10), 'A', 'a@example.com', doctor_id='dr-rahman')
    for said in ['did you get my email?', 'yes, confirm it', 'what time is it booked for']:
        assert await read_choices(s, T, 'c', said) is None
    assert p.customer_email == 'a@example.com'


@pytest.mark.asyncio
async def test_saying_change_the_mail_on_a_call_reopens_the_box():
    from zovak.voice.sideband import VoiceSideband
    s = DemoScheduler()
    voice = VoiceSideband(session_id='live', conversation_id='call', api_key='unused', scheduler=s, agent_model='unused')

    async def book(_):
        await s.propose(T, 'call', 'consultation', at(10), 'A', doctor_id='dr-rahman')
        return 'proposed'

    async def never(_):
        raise AssertionError('handled without the AI')

    await voice._handle_turn('Dr. Rahman at 10, I am A', book)
    state = await voice.booking_state()
    await voice.submit_email(request_id=state['email_request_id'], email='a@example.com')
    voice.needs_readback = False  # on a real call the details were already read out when the email arrived
    reply = await voice._handle_turn('i would like to change the mail', never)
    assert 'reopened the email box' in reply
    assert (await voice.booking_state())['stage'] == 'email_required'


@pytest.mark.asyncio
async def test_after_booking_asking_about_times_shows_no_new_booking_card():
    from zovak.services.doctor_choice import read_choices
    s = DemoScheduler()
    await read_choices(s, T, 'c', 'Dr. Farhan Karim please')
    first = (await _state(s))['chosen_doctor']['slots'][0]['times'][0]
    await read_choices(s, T, 'c', slot_request(datetime.fromisoformat(first), 'Dr. Farhan Karim'))
    p = await s.propose(T, 'c', 'skin_consultation', datetime.fromisoformat(first), 'A', 'a@example.com',
                        doctor_id='dr-karim')
    await s.confirm(T, 'c', p.id)
    assert s.chosen_doctor(T, 'c') is None and s.picked_time(T, 'c') is None  # picks were for this booking
    await read_choices(s, T, 'c', 'what times does Dr. Karim have now?')
    state = await _state(s)
    assert state['stage'] == 'booked' and state['chosen_doctor'] is None  # just the booked card
    s.offer_doctors(T, 'c', GPS)  # a second appointment still goes through doctor cards
    assert len((await _state(s))['doctor_options']) == 2


# ── After booking: one time change; "never mind" ────────────────────────────

async def booked_with_karim(s, conv='c'):
    p = await s.propose(T, conv, 'skin_consultation', at(11), 'A', 'a@example.com', doctor_id='dr-karim')
    await s.confirm(T, conv, p.id)
    return p


@pytest.mark.asyncio
async def test_change_time_after_booking_shows_buttons_and_a_tap_prepares_the_move():
    from zovak.services.doctor_choice import read_choices
    s = DemoScheduler()
    booking = await booked_with_karim(s)
    assert (await _state(s))['booking']['can_change_time'] is True
    assert await read_choices(s, T, 'c', "I'd like to change my appointment time.") == 'move'
    state = await _state(s)
    assert state['stage'] == 'booked' and state['chosen_doctor']['purpose'] == 'move'
    times = [t for d in state['chosen_doctor']['slots'] for t in d['times']]
    assert at(11).isoformat() not in times  # its own current time isn't offered
    new_time = datetime.fromisoformat(times[0])
    await read_choices(s, T, 'c', slot_request(new_time, 'Dr. Farhan Karim'))
    state = await _state(s)
    assert state['stage'] == 'move_pending' and booking.start == at(11)  # prepared, not moved
    await s.apply_change(T, 'c', state['change']['id'])  # "Confirm move"
    assert booking.start == new_time
    state = await _state(s)
    assert state['booking']['can_change_time'] is False and state['chosen_doctor'] is None
    assert await read_choices(s, T, 'c', 'can I change the time again?') == 'move-used'
    with pytest.raises(SchedulingError, match='already changed once'):
        await s.propose_move(T, 'c', at(16, 30))


@pytest.mark.asyncio
async def test_keep_current_time_ends_the_change_without_using_it():
    from zovak.services.doctor_choice import read_choices
    s = DemoScheduler()
    booking = await booked_with_karim(s)
    await read_choices(s, T, 'c', 'change the time please')
    first = (await _state(s))['chosen_doctor']['slots'][0]['times'][0]
    await read_choices(s, T, 'c', slot_request(datetime.fromisoformat(first), 'Dr. Farhan Karim'))
    change = await s.latest_change(T, 'c')
    await s.discard_change(T, 'c', change.id)  # "Keep current time"
    state = await _state(s)
    assert state['stage'] == 'booked' and state['chosen_doctor'] is None
    assert booking.time_changes == 0 and state['booking']['can_change_time'] is True


@pytest.mark.asyncio
@pytest.mark.parametrize('said', ['no forget it', 'never mind', "I'm not interested", 'no thanks'])
async def test_never_mind_clears_cards_and_drops_unconfirmed_only(said):
    from zovak.services.doctor_choice import read_choices
    s = DemoScheduler()
    s.offer_doctors(T, 'c', GPS)
    await read_choices(s, T, 'c', 'Rahman please')
    assert await read_choices(s, T, 'c', said) == 'dropped'
    state = await _state(s)
    assert state['doctor_options'] == [] and state['chosen_doctor'] is None
    booking = await booked_with_karim(s, 'd')  # a confirmed booking is never touched
    await read_choices(s, T, 'd', said)
    assert (await s.latest_booking(T, 'd')).id == booking.id
