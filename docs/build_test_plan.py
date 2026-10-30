"""Build docs/ZOVAK_Test_Plan.pdf: hands-on test cases for website chat, website voice,
phone calls and the staff portal.

Run from zovak-backend:  python docs/build_test_plan.py

Uses the same style as the documentation. Update the cases when a feature changes.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

from build_documentation import CHROME, CSS, ROOT

OUT_HTML = ROOT / "docs" / "ZOVAK_Test_Plan.html"
OUT_PDF = ROOT / "docs" / "ZOVAK_Test_Plan.pdf"

EXTRA_CSS = """
table.cases td:first-child { font-weight: 700; white-space: nowrap; color: #0f766e; }
table.cases td:nth-child(2) { width: 42%; }
table.cases td.mark { width: 70px; border-left: 1px dashed #b8c7cc; }
table.cases tr { break-inside: avoid; }
.say { font-style: italic; color: #0f4c5c; }
.note { background: #fff7e6; border-left: 3px solid #f59e0b; padding: 8px 12px; margin: 10px 0; }
"""


def say(text: str) -> str:
    return f'<span class="say">“{text}”</span>'


def cases(prefix: str, *rows: tuple[str, str], start: int = 1) -> str:
    """A test table: ID, what to do, what should happen, an empty Pass/Fail box to fill in."""
    trs = "".join(
        f"<tr><td>{prefix}-{i:02d}</td><td>{do}</td><td>{expect}</td><td class=\"mark\">☐ Pass<br>☐ Fail</td></tr>"
        for i, (do, expect) in enumerate(rows, start)
    )
    return ('<table class="cases"><thead><tr><th>#</th><th>What to do</th><th>What should happen</th>'
            f"<th>Result</th></tr></thead><tbody>{trs}</tbody></table>")


def content() -> str:
    s = []
    add = s.append

    add(f"""
<div class="cover">
  <p class="kicker">ZOVAK · Nofa Health</p>
  <h1>Test plan</h1>
  <p class="sub">Hands-on test cases for the new features: choosing a doctor, booking,
  moving and cancelling, call rules, and the staff side (running late, patient updates).</p>
  <p class="meta">Built {date.today().strftime('%B %d, %Y')}. Tick Pass or Fail and write notes next to anything odd.</p>
</div>
<h2>Contents</h2>
<ol class="toc">
<li>Before you start</li><li>Website chat</li><li>Website voice call</li><li>Phone call and the SMS form</li>
<li>Staff side: portal and running late</li><li>Staff assistant</li><li>End-to-end scenario</li>
<li>Security quick checks</li><li>Not covered right now</li>
</ol>
""")

    add("""
<h2>1. Before you start</h2>
""" + table_setup() + """
<div class="note"><b>Times are Dhaka time.</b> Doctors work: Dr. Rahman 9–5 (lunch 1–2), Dr. Chowdhury
10–6 (lunch 2–3), Dr. Sultana 9–3 (lunch 12–1), Dr. Karim 11–7 (lunch 3–4). The “running late” tests only
find patients if that doctor still has visits <b>today</b>, so run section 5 during their working hours
(Dr. Karim works latest). Outside hours, demo bookings are made for tomorrow.</div>
<div class="note"><b>Fresh start:</b> everything is kept in memory. Restarting uvicorn clears all bookings,
loads new demo bookings around the current time, and logs Google out (reconnect it).</div>
<p>Each case says what to do or say, and what should happen. Words in <span class="say">blue italics</span>
are examples of what to type or say; your own wording is fine.</p>
""")

    add("""
<h2>2. Website chat</h2>
<p>Open <b>http://localhost:3000</b> → <b>Talk to Nofa</b> → <b>Chat</b>. Use a private window for a
new conversation.</p>
<h3>2.1 Choosing a doctor</h3>
""" + cases("C",
        ("Open the chat.",
         "Greeting: “Tell me what you need, and I'll find the right doctor…”, with buttons <b>I need to see a "
         "doctor</b>, <b>What services do you offer?</b>, <b>Change or cancel my booking</b>."),
        ("Tap <b>What services do you offer?</b>",
         "Nofa describes the kinds of care (general, children's, skin) and asks what you need. No list of "
         "every doctor, no cards."),
        (f"Type {say('I want to book an appointment')}.",
         "Nofa first asks what kind of doctor or what problem it is. She doesn't offer times yet."),
        (f"Type {say('I have a fever and a cough')}.",
         "Two doctor cards: Dr. Ayesha Rahman and Dr. Imran Chowdhury (General Practice), with services, "
         "next free time and a <b>Choose</b> button. Nofa gives no diagnosis."),
        (f"New chat: {say('My 5 year old needs a vaccine')}.",
         "Only Dr. Nusrat Sultana (Pediatrics) is offered."),
        (f"New chat: {say('I have a rash on my arm')}.",
         "Only Dr. Farhan Karim (Dermatology) is offered."),
        (f"New chat: {say('I need a dentist')} (also try an eye test).",
         "Nofa says clearly the clinic doesn't offer that and lists what it does offer. No cards."),
        ("Tap <b>Choose</b> on Dr. Rahman.",
         "A message is sent for you (\"I'd like to book with Dr. Ayesha Rahman…\"). The cards are replaced by "
         "a <b>Doctor selected</b> card. Nofa asks for a day/time and only offers Dr. Rahman's free times."),
        ("With a doctor selected, look at the card.",
         "Day tabs (next 3 days with openings) and time buttons. No lunch times, no booked or past times."),
        ("Tap a time (e.g. 11:00am).",
         "A message “I'd like … at 11:00 AM with Dr. …” is sent for you. The card collapses to “Time selected · "
         "… 11:00 AM” with <b>Change time</b>. Nofa only asks for what's missing (your name); then the booking "
         "card with the email box appears. Nothing is booked yet."),
        (f"New chat: {say('I would like to book with Dr. Farhan Karim for a skin rash')}",
         "Straight to <b>Doctor selected</b> with his time buttons (no doctor cards first)."),
        (f"After picking a time, type {say('can I change the time?')}, then {say('I want a different doctor')}.",
         "The time buttons come back; then the doctor choice starts again."),
        ("Go all the way to <b>Review your appointment</b> (name + email), then type "
         f"{say('can i chane the time')} (typo on purpose), and tap another time.",
         "The time buttons come back for the same doctor; the new tap goes straight to Review with your name "
         "and email kept. No email box again."),
        ("In a fresh browser (or private window) click <b>Talk to Nofa</b>.",
         "A “Private demo” box asks for the team code; a wrong code is refused. Nothing works without it."),
        ("Enter the team code.",
         "Amartya's note appears on bright paper; <b>I understand…</b> opens the chat with the floating test scripts."),
        ("On any card, tap the small ✕.",
         "The card hides (nothing is cancelled); it comes back when it changes or you ask about your booking."),
        ("Send any message and watch Nofa's reply.",
         "The words appear as she writes them (streaming), not all at once after a pause."),
        ("Tap a time.",
         "Nofa confirms that time in a few words and asks only for your name; she doesn't say “select it below”."),
        ("After booking, ask “what services do you offer?”, then “show my booking status”.",
         "No card after the first; the booked card after the second."),
        ("On the booked card, tap <b>Change time</b>, tap a new time, then <b>Confirm move</b>.",
         "“Pick your new time” → “Move this appointment?” → booked at the new time. The Change time button is gone; "
         "the card says another change needs the clinic. (Tapping Keep instead doesn't use up the change.)"),
        ("With doctor cards or time buttons showing, type “no, forget it”.",
         "The cards go away; nothing booked is changed."),
        ("After the booking is confirmed, ask “what are the available times now?”.",
         "Nofa answers; only the green booked card shows — no new “Time selected” card."),
        ("Close the chat panel and reopen it; then refresh the page and open the chat.",
         "The same conversation and its card come back both times (a new browser tab starts fresh)."),
        (f"On the review card, type {say('i would like to change the mail')}.",
         "The email box opens again for the same appointment; type a new email and Review shows it."),
        ("Get to the review card, wait 6 minutes, then change the time.",
         "Still no email box: the email is remembered for the whole chat. The card shows your real email."),
        ("Send a few messages that change the card.",
         "Each new card appears together with Nofa's reply; the previous card never flashes up in between."),
        ("On the review card, type a new time instead (e.g. “5 pm”).",
         "A new review card for 5 PM with the same email."),
        ("Tap <b>Change time</b>, then another time.",
         "The buttons come back with the current time highlighted; the new tap collapses the card to the new time."),
        (f"New chat: get the GP cards, then type {say('Chowdhury please')} instead of tapping.",
         "Same as tapping: <b>Doctor selected</b> shows Dr. Chowdhury. Typing “Rahman or Chowdhury” or "
         "“not Rahman” keeps the cards (Nofa asks which one)."),
        (f"With a doctor selected, type {say('Actually, I need a skin doctor')}.",
         "The selected card goes; Dr. Karim's card appears."),
        (f"Ask for Dr. Rahman {say('tomorrow at 1:30 PM')}, then Dr. Chowdhury {say('tomorrow at 9 AM')}.",
         "Both are refused with other times offered: 1:30 is Rahman's lunch; Chowdhury starts at 10."),
        (f"Type {say('next Tuesday at 11')}.",
         "Nofa understands the date herself and doesn't ask for the year."),
    ) + """
<h3>2.2 Booking</h3>
""" + cases("C",
        ("Give your name and a free time with the chosen doctor.",
         "The card shows the <b>email box</b>. Nothing is booked yet."),
        ("Type a wrong email (<code>abc@</code>), then your real email in the box.",
         "Wrong email is rejected. Real email: card shows service, doctor, date and time, with "
         "<b>Confirm booking</b> and <b>Change email</b>."),
        ("Tap <b>Confirm booking</b>.",
         "Green booked card. Google Calendar has the event (title includes the doctor) and the invite "
         "email arrives."),
        ("In a second private window, ask for <i>any GP</i> at the exact same time and book it.",
         "It is booked with the <b>other</b> GP: two GPs can see patients at the same time."),
        ("In a third window, ask for a GP at that same time again.",
         "Not available: both GPs are busy. Nofa offers other times."),
        ("(Optional, needs Resend) Book a visit about 80 minutes from now.",
         "A reminder email arrives about 1 hour before the visit, once only."),
    start=33) + """
<h3>2.3 Moving and cancelling (same chat as the booking)</h3>
""" + cases("CM",
        (f"Type {say('Can we move it to 4 PM?')}",
         "Card shows the old time crossed out → new time, with <b>Confirm move</b> and <b>Keep</b>. "
         "Nothing has moved yet."),
        ("Tap <b>Confirm move</b>.",
         "Booking moves. Google event moves and Google emails the update."),
        ("Ask to move again, then tap <b>Keep</b>.",
         "Nothing changes."),
        (f"Type {say('Please cancel my appointment')} → <b>Cancel appointment</b>.",
         "Grey cancelled card. Google event removed; cancellation email arrives. The slot is free again."),
        (f"Ask to cancel, then type {say('actually keep it')}.",
         "Booking stays."),
    ) + """
<h3>2.4 Staying on topic</h3>
""" + cases("CG",
        (f"Type {say('Write me a poem about cats')}.",
         "Polite refusal; Nofa steers back to appointments."),
        (f"Type {say('What medicine should I take for my fever?')}",
         "No medical advice or diagnosis; suggests booking (or emergency care if urgent)."),
    ))

    add("""
<h2>3. Website voice call</h2>
<p><b>Talk to Nofa</b> → <b>Voice call</b> → <b>Start call</b> (allow the microphone). Speak normally;
watch the card under the transcript.</p>
<h3>3.1 Booking by voice</h3>
""" + cases("V",
        ("Start the call.",
         "Status changes to listening, the timer runs and Nofa greets you."),
        (f"Say {say('Which doctors do you have?')}",
         "Nofa describes the kinds of care and asks about your problem. She doesn't read out doctors' names; no cards."),
        (f"Say {say('I want to book, I have a fever')}.",
         "Nofa names only the two GPs. A slim list appears (“Say the name of the doctor you'd like”) and the "
         "transcript below stays visible; with more doctors the list scrolls inside itself."),
        (f"Say {say('Dr. Rahman please')}.",
         "The cards change to <b>Doctor selected</b>: Dr. Rahman, with day tabs and time buttons."),
        ("Tap a time button during the call.",
         "Nofa says it back aloud and asks for your name. Saying a time out loud still works too."),
        (f"Say {say('Do you have a dentist?')}",
         "Nofa says the clinic doesn't offer it and lists what it offers."),
        (f"Say {say('Dr. Chowdhury tomorrow at 11, my name is …')}. Also try saying your email out loud.",
         "The card shows the <b>email box</b>. Nofa asks you to type the email; the spoken email is ignored."),
        ("Type your email in the box.",
         "Nofa reads the details back and asks you to confirm. Nothing is booked yet."),
        (f"Say {say('Yes, but change the time')}.",
         "NOT booked. Nofa asks for the new time."),
        (f"Say {say('Yes, confirm my…')} and keep talking.",
         "NOT booked (the sentence was unfinished). Your next words are treated as a new request."),
        (f"Say {say('Yes, confirm it')} and stop talking.",
         "After a short pause: booked, green card, Nofa says it's booked and asks if there's anything else."),
        ("Repeat a booking but tap <b>Confirm booking</b> instead of speaking.",
         "Same result; Nofa also says the result on the call."),
    ) + """
<h3>3.2 Moving and cancelling by voice</h3>
""" + cases("VM",
        (f"Say {say('Move it to 3 PM')}.",
         "Move card appears; Nofa asks you to tap Confirm move or say “Yes, confirm the move”."),
        (f"Say {say('Yes, confirm the move')}.",
         "Moved; Google event updated."),
        (f"Say {say('Cancel my appointment')} → {say('Yes, cancel it')}.",
         "Cancelled; grey card."),
    ) + """
<h3>3.3 Call rules</h3>
""" + cases("VG",
        (f"After an action, say {say('No, that is all')}.",
         "Nofa says goodbye, the mic is muted and the call ends."),
        (f"After booking, say {say('Great, thanks, bye')}.",
         "“You're all set with Dr. … on … at …. Thanks for calling Brightside Family Clinic. Take care, "
         "goodbye!” Then the call ends by itself."),
        (f"Without booking anything, say {say('I have to go')} or {say('you can hang up')}.",
         "“Thanks for calling Brightside Family Clinic. Take care, goodbye!” and the call ends."),
        (f"While the email box or the confirm step is showing, say {say('bye')}.",
         "Nofa asks once: “Before you go, your appointment isn't booked yet…”. Say “bye” again: "
         "“No problem, I haven't booked anything…” and the call ends. Nothing is booked."),
        (f"Say {say('okay')} or {say('thanks')} on their own, or {say('bye, but first move it to 3')}.",
         "The call does NOT end."),
        ("Stay silent (no email box open).",
         "After 10 s: a warning. After 10 s more: goodbye and the call ends."),
        ("Stay silent while the email box is open.",
         "The call waits up to 2 minutes (typing takes time)."),
        ("Only cough, “hmm” or “uh” several times.",
         "Not treated as speech. On the 3rd noise-only turn (or right after the warning) Nofa ends the call."),
        ("Ask three off-topic things in one call.",
         "Polite refusals; the 3rd ends the call."),
        ("Prepare bookings/moves/cancellations again and again (5 times).",
         "The call ends after 4 prepared actions."),
        ("Press <b>End call</b> at any time.",
         "The call ends at once."),
    ))

    add("""
<h2>4. Phone call and the SMS form</h2>
<p>Call the Telnyx number from your phone. Keep the uvicorn terminal visible. Form links appear in the
test inbox <b>http://127.0.0.1:8000/api/v1/dev/sms-outbox</b> and, if set up, as an ntfy notification.</p>
<h3>4.1 The call</h3>
""" + cases("P",
        ("Call the number.",
         "Nofa answers. The log shows “Accepted phone call…”, “Nofa is speaking” and “Caller audio is arriving”."),
        (f"Say {say('Which doctors do you have?')}",
         "Nofa describes the kinds of care and asks about your problem; she does not read out all the doctors."),
        (f"Say {say('I need to see a doctor for my skin')}.",
         "Nofa names only Dr. Karim and his service (no cards on a phone)."),
        (f"Say {say('Do you do X-rays?')}",
         "Nofa says it isn't offered and lists what is."),
        ("Pick a time with the doctor and give your name.",
         "Nofa reads back the doctor, service, day and time. She doesn't ask for your email by voice."),
        (f"Say {say('Yes, but make it later')}.",
         "No form is sent; Nofa asks for the new time."),
        (f"Say {say('Yes, confirm it')}.",
         "Nofa says she sent a text and that you can still change or cancel from the form, then hangs up. "
         "The booking is NOT confirmed yet."),
        (f"New call, say {say('Thanks, bye')}.",
         "A warm goodbye and Nofa hangs up by herself."),
        ("Stay silent on a new call.",
         "Warning after 10 s; goodbye and a real hang-up after 10 s more."),
    ) + """
<h3>4.2 The SMS form</h3>
""" + cases("F",
        ("Open the form link.",
         "Shows the doctor, service and time with a 2:00 countdown; name filled in; email empty."),
        ("While the countdown runs, try to book the same doctor and time in website chat.",
         "That time isn't offered: the form holds it."),
        ("Look at the other times in the form.",
         "Only the same doctor's free times."),
        ("Add your email → <b>Confirm appointment</b>.",
         "Booked; Google invite arrives. Opening the link again only shows the result."),
        ("New call → form → pick a different time → confirm.",
         "Booked at the new time, same doctor."),
        ("New call → form → <b>Cancel booking</b>.",
         "Nothing is booked; the time is free again."),
        ("New call → form → wait over 2 minutes.",
         "The form says the time ran out; the slot is free again."),
    ))

    add("""
<h2>5. Staff side: portal and running late</h2>
<p>Open <b>http://localhost:3000/staff</b>. Demo sign-ins: Dr. Rahman 1111, Dr. Chowdhury 2222,
Dr. Sultana 3333, Dr. Karim 4444, Rina Akter (reception) 5555, Tanvir Hasan (manager) 9999.</p>
<h3>5.1 Sign in and schedule</h3>
""" + cases("S",
        ("Open /staff.",
         "The staff sign-in page. The patient Nofa button is NOT shown on this page."),
        ("Sign in with a wrong PIN, then 5 wrong PINs in a row for one name.",
         "Wrong PIN refused. After 5 tries that name is locked for 5 minutes (other names still work)."),
        ("Sign in as Dr. Rahman (1111) → <b>Schedule</b>.",
         "“Your schedule”: only Dr. Rahman's visits, Today/Tomorrow, past visits greyed out."),
        ("Sign out, sign in as reception (5555) or manager (9999).",
         "All four doctors' schedules."),
        ("Book something in website chat or by phone, then watch the schedule (no refresh needed).",
         "Within 5 seconds the new booking appears under the right doctor with the patient's name."),
        ("Book with Dr. Chowdhury for a day next week. Sign in as Dr. Chowdhury (2222) and pick that day "
         "with the date picker (or ›).",
         "The booking is there. Dr. Rahman (1111) on that day doesn't see it; reception (5555) does."),
        ("Use ‹ and › to step through days.",
         "The date and the label (e.g. “Monday, Nov 2”) change together; Today/Tomorrow light up when they match."),
    ) + """
<h3>5.2 Running late: preview</h3>
""" + cases("SL",
        ("As a doctor, open <b>Running late</b>.",
         "Only their own name; “How late is the doctor?” choice."),
        ("Pick 30 minutes → <b>See who is affected</b>.",
         "A list of affected patients: old time → new time and each patient's options, and “Counted from …”. "
         "“Nobody is notified until you confirm.” The Schedule tab is unchanged."),
        ("In the evening (after the doctor's day), Late from <b>Now</b>, 30 min.",
         "Counted from <b>tomorrow's</b> start time. If the first patient is later than start + 30 min: "
         "“Nobody is affected” with the reason (first patient's name and time). Nothing to send."),
        ("Late from <b>A specific time</b>: tomorrow 1:00 PM, 30 min (with a 1:00 patient).",
         "Only the 1:00 patient (and anyone right after who's reached) moves; earlier patients are untouched."),
        ("Check the list against the schedule.",
         "Only patients the delay reaches: once there's a free gap (or lunch) big enough, later patients "
         "are not listed. Shifted visits never land in lunch."),
        ("Check the options of the first patient.",
         "Best first: same time with another free doctor (if one is free), the shifted time (default), "
         "the doctor's other times only after they arrive, then cancel."),
        ("As reception, pick a different doctor and a delay near closing time (e.g. 90 min).",
         "Patients pushed past closing show “needs a new time” with the next free slot as default."),
        ("Press <b>Discard</b>.",
         "Nothing changes; nobody is notified."),
    ) + """
<h3>5.3 Running late: notify and patient choice</h3>
""" + cases("SN",
        ("Preview again → <b>Notify N patients</b>.",
         "Schedule shows the new times. Each patient gets a message with a one-time /update link (test inbox, "
         "ntfy, WhatsApp/email where available). <b>Patient updates</b> shows “Waiting for patient”."),
        ("Open one patient's /update link.",
         "Says the doctor is running about N minutes late and lists the options."),
        ("Choose “same time with another doctor”.",
         "Status becomes Rescheduled; the schedule shows the visit under the other doctor at the original time."),
        ("For another patient choose the new time; for a third choose cancel.",
         "Statuses: Kept new time and Cancelled; the cancelled visit leaves the schedule."),
        ("Open a used link again.",
         "Shows the choice already made; can't choose twice."),
        ("Leave one patient without a reply for 15 minutes.",
         "Shown as no reply; they keep the new time."),
        ("Try to notify the same plan twice (refresh and press again if possible).",
         "Refused: already sent."),
    ))

    add("""
<h2>6. Staff assistant</h2>
<p>Staff portal → <b>Assistant</b> tab.</p>
""" + cases("SA",
        (f"As Dr. Rahman: {say('What does my schedule look like tomorrow?')}",
         "A list of only Dr. Rahman's visits."),
        (say("I’m running 40 minutes late"),
         "Nofa summarises who is affected and their options and asks for confirmation. Nothing is sent. "
         "The reply is written out word by word and the chat scrolls down by itself."),
        (f"In the evening: {say('I’ll be 30 minutes late for my 1:00 tomorrow')}",
         "Only the 1:00 patient is affected, moved to 1:30, with other options. Nothing sent until you confirm."),
        ("Reload the page while signed in.",
         "The chat is still there. Ask a follow-up: Nofa remembers it."),
        ("Sign out and sign in again (or restart the backend and sign in).",
         "A clean chat, and Nofa doesn't mention any earlier delay plans."),
        (f"Ask {say('What appointments do I have tomorrow?')}",
         "Only the list of appointments, with no unasked remarks about delay plans."),
        (f"In the evening: {say('I’m running 30 minutes late')} (first patient tomorrow later than start + 30)",
         "Nofa says nobody is affected and why; she does NOT offer to “apply” or send anything."),
        (f"{say('Yes, send it')}",
         "Sent; the plan appears in Patient updates and patients get their links."),
        (f"As Dr. Rahman: {say('Dr. Karim is 30 minutes late')}",
         "A doctor can only act for themselves: it plans Dr. Rahman's own schedule or refuses."),
        (f"As reception: {say('Dr. Karim is 30 minutes late')} → confirm.",
         "Works for Dr. Karim."),
    ))

    add("""
<h2>7. End-to-end scenario</h2>
<p>One story that touches every part. Run it during Dr. Rahman's working hours.</p>
""" + cases("E",
        ("Call by phone and book Dr. Rahman for later today through the SMS form, with your real email.",
         "Booked; Google invite arrives; the booking keeps your phone number."),
        ("In the portal as Dr. Rahman: Running late 30 min → See who is affected.",
         "You are in the list (if the delay reaches your time)."),
        ("Notify patients.",
         "Your phone (ntfy) or the test inbox gets the /update link."),
        ("Open the link and choose Dr. Chowdhury at your original time.",
         "Google event updates with Dr. Chowdhury; portal shows Rescheduled; schedule shows it under Dr. Chowdhury."),
        ("Ask Nofa about your booking by phone.",
         "Known limit today: a new call is a new conversation, so Nofa can't see it yet (comes with customer tracking)."),
    ))

    add("""
<h2>8. Security quick checks</h2>
<p>Use your ngrok address (the public one) for these.</p>
""" + cases("X",
        ("Open https://YOUR-NGROK/api/v1/staff/directory and /api/v1/dev/sms-outbox.",
         "Refused: only the phone webhook, WhatsApp webhook, /form/ and /update/ pages are public."),
        ("Open /update/not-a-token and a made-up /form/ link.",
         "Not found."),
        ("Call the staff schedule without signing in (or after signing out).",
         "Refused (401)."),
        ("In website chat, ask Nofa to “confirm my booking” without tapping a button.",
         "Nothing is booked by the AI's text alone; you still need the button (or a clear spoken yes on a call)."),
    ))

    add("""
<h2>9. Not covered right now</h2>
<ul>
<li><b>WhatsApp chat and calls:</b> paused while Meta reviews the locked business account (error 131031).
Re-add these tests once it is unlocked.</li>
<li><b>Real SMS to Bangladesh:</b> needs a local gateway; use the test inbox and ntfy.</li>
<li><b>Saved bookings across restarts and across channels:</b> waits for the customer tracking plan.</li>
</ul>
""")
    return "\n".join(s)


def table_setup() -> str:
    rows = [
        ("Backend", "conda env Crewai: <code>uvicorn zovak.main:app --reload --host 127.0.0.1 --port 8000</code>"),
        ("Website", "<code>npm run dev</code> in serena-health → http://localhost:3000"),
        ("Google Calendar", "Open http://127.0.0.1:8000/api/v1/auth/google/start after every backend restart"),
        ("Phone tests", "<code>ngrok http 8000</code>; PUBLIC_BASE_URL and the OpenAI webhook use the current ngrok address"),
        ("Texts", "Test inbox http://127.0.0.1:8000/api/v1/dev/sms-outbox; ntfy app subscribed to your topic"),
        ("Emails", "Use your own email so you receive invites, updates and reminders"),
        ("Automated tests", "<code>python -m pytest -q</code> in zovak-backend should pass before manual testing"),
    ]
    trs = "".join(f"<tr><td><b>{a}</b></td><td>{b}</td><td class=\"mark\">☐ Ready</td></tr>" for a, b in rows)
    return f'<table class="cases"><thead><tr><th>Item</th><th>How</th><th>Check</th></tr></thead><tbody>{trs}</tbody></table>'


def main() -> None:
    page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f"<title>ZOVAK test plan</title><style>{CSS}{EXTRA_CSS}</style></head>"
            f"<body>{content()}</body></html>")
    OUT_HTML.write_text(page)
    if not Path(CHROME).exists():
        sys.exit(f"Wrote {OUT_HTML}. Google Chrome not found, so no PDF was printed.")
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={OUT_PDF}", OUT_HTML.as_uri()],
                   check=True, capture_output=True, timeout=120)
    print(f"Wrote {OUT_PDF}")


if __name__ == "__main__":
    main()
