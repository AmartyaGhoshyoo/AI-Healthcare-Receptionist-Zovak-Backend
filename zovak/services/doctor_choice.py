"""Notice when the patient picks one of the doctors Nofa offered, or one of their times.

Plain code, not the AI, so the doctor cards always go away once a doctor is picked
(tapping Choose sends "I'd like to book with Dr. …", or the name is typed or said), and
the time buttons collapse once a time is tapped ("I'd like Friday, October 30 at 2:00 PM
with Dr. …").
"""
from __future__ import annotations

import re
from datetime import datetime

from zovak.agents.memory import note_for_agent
from difflib import SequenceMatcher

# Words that mean the patient is turning a doctor down or asking for someone else.
_NOT_A_PICK = re.compile(r"\b(not|neither|don'?t|no|other|another|different|instead of|except)\b", re.I)


def _names(doctor) -> list[str]:
    """Distinctive name words, e.g. "Dr. Ayesha Rahman" -> ["ayesha", "rahman"]."""
    return [w for w in re.findall(r"[a-z]+", doctor.name.lower()) if w != "dr" and len(w) >= 4]


def _mentions(words: list[str], doctor) -> bool:
    # Close matches too: speech-to-text may write "Chowdry" or "Rahmen".
    return any(SequenceMatcher(None, w, name).ratio() >= 0.8 for w in words for name in _names(doctor))


def pick_offered_doctor(scheduler, tenant_id: str, conversation_id: str, text: str) -> str | None:
    """If the message names exactly one doctor, record the choice and return its id.

    Among the offered cards if there are any; otherwise any clinic doctor, so "book me
    with Dr. Farhan Karim" picks him straight away, with no cards first.
    """
    offered = getattr(scheduler, "offered_doctors", lambda *_: [])(tenant_id, conversation_id)
    booked = getattr(scheduler, "has_booking", lambda *_: False)(tenant_id, conversation_id)
    # After a booking, a doctor's name alone is talk about it; a new booking goes through cards.
    candidates = offered or ([] if booked else list(getattr(scheduler, "doctors", {})))
    if not candidates or _NOT_A_PICK.search(text):
        return None
    words = re.findall(r"[a-z]+", text.lower())
    matches = [d for d in candidates if _mentions(words, scheduler.doctors[d])]
    if len(matches) != 1:
        return None  # none or ambiguous: Nofa asks, the cards stay
    scheduler.choose_doctor(tenant_id, conversation_id, matches[0])
    return matches[0]


# The sentence a tapped time button sends (booking_state.slot_request).
_TAPPED_TIME = re.compile(r"I'd like \w+, (\w+ \d{1,2}) at (\d{1,2}:\d{2} [AP]M) with ", re.I)


async def pick_tapped_time(scheduler, tenant_id: str, conversation_id: str, text: str) -> datetime | None:
    """If the message is a tapped time for the chosen doctor and it's still free, remember it."""
    from zovak.services.booking_state import free_days

    found = _TAPPED_TIME.search(text)
    doctor_id = getattr(scheduler, "chosen_doctor", lambda *_: None)(tenant_id, conversation_id)
    if not found or doctor_id is None:
        return None
    for day in await free_days(scheduler, tenant_id, conversation_id, doctor_id):
        for iso in day["times"]:
            start = datetime.fromisoformat(iso)
            if f"{start:%B} {start.day}" == found[1] and start.strftime("%I:%M %p").lstrip("0") == found[2].upper():
                scheduler.pick_time(tenant_id, conversation_id, start)
                if getattr(scheduler, "moving_booking", lambda *_: None)(tenant_id, conversation_id):
                    # Changing a confirmed booking: prepare the move now; they confirm or keep.
                    try:
                        await scheduler.propose_move(tenant_id, conversation_id, start)
                    except Exception:
                        return None
                return start
    return None


# "Can I change the time?", "another slot", "a different day" / "a different doctor".
# Typos too ("chane", "chnage"), but not "check": checking a time isn't changing it.
_CHANGE = r"(?:change|chane|chnage|chang|chage|cange|switch|move|another|different|other|earlier|later|new|reschedule)"
_NEW_TIME = re.compile(rf"\b{_CHANGE}\b.*\b(time|slot|day|date)s?\b", re.I)
# "change the mail", "wrong email", "use a new e-mail".
_NEW_EMAIL = re.compile(rf"\b({_CHANGE}|wrong|update|edit|correct|fix|incorrect)\b.*\b(e-?)?mail\b"
                        r"|\b(e-?)?mail\b.*\b(wrong|incorrect|typo|mistake)\b", re.I)


def wants_new_email(text: str) -> bool:
    """The patient wants to change the email they typed (any channel)."""
    return bool(_NEW_EMAIL.search(text))


# "No, forget it", "never mind", "not interested": drop the doctor/time choice in progress.
_NEVER_MIND = re.compile(r"\b(forget (it|that|about it)|never ?mind|not interested|no thanks|leave it|"
                         r"skip (it|that)|don'?t want (it|to book|an appointment))\b", re.I)

_NEW_DOCTOR = re.compile(rf"\b({_CHANGE}|someone else)\b.*\b(doctor|dr|specialist)s?\b"
                         r"|\bsomeone else\b", re.I)


async def reopen_choice(scheduler, tenant_id: str, conversation_id: str, text: str, *,
                        emails: bool = True) -> str | None:
    """The patient wants to change what they picked: bring back the time buttons or doctor choice.

    Also while a proposal is waiting for confirmation: it is set aside (name and email kept)
    and the same doctor's time buttons come back.
    """
    if _TAPPED_TIME.search(text) or not hasattr(scheduler, "chosen_doctor"):
        return None  # a tapped time is a pick, not a change request
    if _NEVER_MIND.search(text):
        dropped = await scheduler.latest_pending(tenant_id, conversation_id)
        if dropped is not None:
            await scheduler.discard(tenant_id, conversation_id, dropped.id)  # nothing was booked
        change = await scheduler.latest_change(tenant_id, conversation_id)
        if change is not None:
            await scheduler.discard_change(tenant_id, conversation_id, change.id)  # booking unchanged
        scheduler.drop_choices(tenant_id, conversation_id)
        return "dropped"
    if _NEW_TIME.search(text) and scheduler.has_booking(tenant_id, conversation_id) and not (
            await scheduler.latest_pending(tenant_id, conversation_id)):
        result = await scheduler.start_time_change(tenant_id, conversation_id)
        await note_for_agent(tenant_id, conversation_id, (
            "The patient wants another time for their confirmed booking. Its doctor's free times are "
            "showing as buttons; when they tap one, the move is prepared for them to confirm."
            if result == "ok" else
            "The patient asked to change the time, but this booking's time was already changed once. "
            "Tell them kindly that another change needs the clinic, and that they can still cancel."))
        return "move" if result == "ok" else "move-used"
    if emails and wants_new_email(text):  # website calls use their own on-screen email box
        pending = await scheduler.latest_pending(tenant_id, conversation_id)
        if pending is not None and pending.customer_email:
            await scheduler.clear_proposal_email(tenant_id, conversation_id, pending.id)  # forgets it too
            await note_for_agent(tenant_id, conversation_id,
                                 "The patient wants a different email, so the email box is showing again. "
                                 "Ask them to type the new address in it; nothing else changes.")
            return "email"
        return None
    if _NEW_DOCTOR.search(text):
        if await scheduler.reopen_times(tenant_id, conversation_id) or scheduler.chosen_doctor(tenant_id, conversation_id):
            scheduler.forget_doctor(tenant_id, conversation_id)
            return "doctor"
        return None
    if _NEW_TIME.search(text):
        if await scheduler.reopen_times(tenant_id, conversation_id):
            await note_for_agent(tenant_id, conversation_id,
                                 "The patient asked for a different time, so the unconfirmed proposal was set "
                                 "aside. The time buttons are showing again; their name and email are kept.")
            return "time"
        if scheduler.picked_time(tenant_id, conversation_id):
            scheduler.unpick_time(tenant_id, conversation_id)
            return "time"
    return None


async def read_choices(scheduler, tenant_id: str, conversation_id: str, text: str, *,
                       emails: bool = True) -> str | None:
    """Every patient message, before the AI runs: changes first, then doctor and time picks.

    Returns "time" or "doctor" when the message reopened that choice.
    """
    reopened = await reopen_choice(scheduler, tenant_id, conversation_id, text, emails=emails)
    pick_offered_doctor(scheduler, tenant_id, conversation_id, text)
    await pick_tapped_time(scheduler, tenant_id, conversation_id, text)
    return reopened
