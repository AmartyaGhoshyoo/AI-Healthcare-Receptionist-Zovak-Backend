"""Build docs/ZOVAK_Documentation.pdf: how ZOVAK + the Nofa website work, with real code.

Run from zovak-backend:  python docs/build_documentation.py

Code excerpts are pulled from the source files at build time (Python by
function/method name, TypeScript by marker), so the PDF never drifts from the
code. Rebuild after every change; add a dated entry to CHANGELOG below.
The PDF is printed with the local Google Chrome in headless mode.
"""
from __future__ import annotations

import ast
import html
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # zovak-backend
SITE = ROOT.parent / "serena-health"  # the Nofa website repo, next to zovak-backend
OUT_HTML = ROOT / "docs" / "ZOVAK_Documentation.html"
OUT_PDF = ROOT / "docs" / "ZOVAK_Documentation.pdf"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


# ── Code extraction ──────────────────────────────────────────────────────────

def py(path: str, name: str, max_lines: int | None = None) -> str:
    """Source of a function, method ('Class.method') or class from a Python file."""
    file = ROOT / path
    source = file.read_text()
    tree = ast.parse(source)
    parts = name.split(".")
    nodes = tree.body
    found = None
    for part in parts:
        found = next((n for n in nodes if isinstance(
            n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == part), None)
        if found is None:
            raise SystemExit(f"Not found: {name} in {path}")
        nodes = getattr(found, "body", [])
    start = (found.decorator_list[0].lineno if found.decorator_list else found.lineno) - 1
    lines = source.splitlines()[start:found.end_lineno]
    indent = len(lines[0]) - len(lines[0].lstrip())
    lines = [line[indent:] for line in lines]
    return block(path, name, lines, max_lines)


def ts(path: str, marker: str, max_lines: int | None = None, label: str | None = None) -> str:
    """A TypeScript/TSX excerpt starting at the line containing marker, to its closing brace."""
    file = SITE / path
    if not file.exists():
        return f'<p class="missing">({html.escape(path)} not found next to zovak-backend)</p>'
    lines = file.read_text().splitlines()
    start = next((i for i, line in enumerate(lines) if marker in line), None)
    if start is None:
        raise SystemExit(f"Marker not found: {marker!r} in {path}")
    depth, end, opened = 0, start, False
    for i in range(start, len(lines)):
        depth += lines[i].count("{") + lines[i].count("(") + lines[i].count("[")
        depth -= lines[i].count("}") + lines[i].count(")") + lines[i].count("]")
        opened = opened or ("{" in lines[i] or "(" in lines[i])
        end = i
        if opened and depth <= 0:
            break
    return block(f"serena-health/{path}", label or marker.strip(), lines[start:end + 1], max_lines)


def block(path: str, name: str, lines: list[str], max_lines: int | None) -> str:
    cut = max_lines is not None and len(lines) > max_lines
    shown = lines[:max_lines] if cut else lines
    code = html.escape("\n".join(shown))
    more = f'\n<span class="cut">… {len(lines) - max_lines} more lines in the file</span>' if cut else ""
    return (f'<figure><figcaption><b>{html.escape(name)}</b> · {html.escape(path)}</figcaption>'
            f"<pre>{code}{more}</pre></figure>")


# ── Small layout helpers ─────────────────────────────────────────────────────

def flow(*steps: str) -> str:
    items = "".join(f'<li><span class="n">{i}</span><div>{s}</div></li>' for i, s in enumerate(steps, 1))
    return f'<ol class="flow">{items}</ol>'


def boxes(*rows: tuple[str, str]) -> str:
    cells = '<div class="arrow">↓</div>'.join(
        f'<div class="box"><b>{a}</b><span>{b}</span></div>' for a, b in rows
    )
    return f'<div class="boxes">{cells}</div>'


def table(head: tuple[str, ...], *rows: tuple[str, ...]) -> str:
    th = "".join(f"<th>{h}</th>" for h in head)
    trs = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"


# ── Change log: add one entry per change, newest first ──────────────────────

CHANGELOG = [
    ("2026-10-30", "Private demo for deployment: one backend setting, site key, access gate, test scripts",
     "One setting: serena-health/src/config/backend.ts holds ZOVAK's address (local 127.0.0.1:8000 by default; "
     "https://stapling-delete-stunner.ngrok-free.dev for Vercel), used by every website→ZOVAK call. Site key: "
     "through the tunnel ZOVAK used to refuse everything but the webhooks and forms; now the website's server "
     "may also reach chat, voice and staff routes if it sends X-Zovak-Site-Key matching SITE_API_KEY "
     "(_guard_public_host; empty key = closed, as before). Access gate: the chat/voice widget and the staff "
     "portal need the team code (DEMO_ACCESS_CODE, server-side only) and Amartya's note accepted; middleware "
     "answers 401 on /api/agent/* and /api/staff/* until then (HttpOnly cookie with a hash of the code, 7 days). "
     "Test scripts: a floating paper with short cost-saving scripts for Chat, Voice, Phone, WhatsApp (paused) and "
     "Staff, next to the widget and on /staff. Every card has a ✕ to hide it (never cancels anything)."),
    ("2026-10-30", "Streaming replies, relevant cards only, one time change after booking, \"never mind\"",
     "Streaming: website chat now streams Nofa's words as the AI writes them (stream_receptionist with "
     "Runner.run_streamed → /api/v1/dev/chat/stream → /api/agent/chat/stream), so text appears within about a "
     "second once she answers; tool calls still run first. Cards: after a reply a card shows only if it changed "
     "or the message is about the booking (time, doctor, email, status…); the last card is kept for the tab so "
     "it is there instantly after a refresh. Tapped time: Nofa treats it as chosen and asks only for what's "
     "missing. One change: a confirmed booking's time can be changed once by the patient (MAX_TIME_CHANGES, "
     "BookingProposal.time_changes, enforced in propose_move for every channel; staff delay moves don't count). "
     "The booked card has Change time with \"You can change the time once after booking\"; it opens Pick your "
     "new time, and a tap prepares the move to Confirm or Keep (Keep doesn't use it up). \"No, forget it\" / "
     "\"never mind\" / \"not interested\" clears doctor and time cards and drops an unconfirmed proposal or "
     "pending change, never a confirmed booking."),
    ("2026-10-30", "Faster first reply, chat kept for the tab, no stray card after booking",
     "First reply: it needed three AI calls in a row (look up doctors, offer them, answer), about 15 s. The "
     "doctors and services are now given to Nofa up front (clinic_catalog), so it is two calls (~5 s measured "
     "in-process; the AI's own speed varies), and the AI library is loaded at backend start instead of during "
     "the first message (~2.5 s). Chat: the conversation id and messages are kept for the browser tab, so "
     "closing the panel or refreshing continues the same chat with its card. Card: confirming a booking clears "
     "the doctor and time picks, and after a booking, asking about free times or naming the doctor no longer "
     "opens a \"Time selected… nothing is booked yet\" card (doctor cards for a second appointment still work)."),
    ("2026-10-30", "\"Change the mail\" reopens the email box",
     "Bug: typing \"i would like to change the mail\" in chat kept the review card, and Nofa pointed at an "
     "email field that wasn't there. Now plain code recognises a request to change the email (\"mail\" or "
     "\"email\", typos included; \"did you get my email?\" doesn't count): in chat and WhatsApp it clears "
     "the email on the waiting proposal (and the remembered one) so the email box shows again for the same "
     "appointment; on website calls the call's own on-screen email box reopens, now also for \"mail\"."),
    ("2026-10-30", "Google-backed scheduler kept wiping the typed email",
     "Bug (seen live): after changing the time, the email box came back every time. The Google-backed "
     "scheduler's propose called the base propose without the email and then overwrote the result with the "
     "email Nofa passed (none), erasing the remembered one. Now it passes the email through and lets the base "
     "scheduler validate it and reuse the one already typed. A new test runs the change-the-time flow on the "
     "Google-backed scheduler itself, not only the demo one."),
    ("2026-10-30", "Name and email remembered per conversation; no card flash; one reply per first message",
     "Bugs: after a pause the email box came back on a time change (proposals expire after 5 minutes and took "
     "the email with them); once the email showed \"already entered on screen\" because Nofa passed a note "
     "instead of an address; the previous card flashed up before the new one; a first message from a page "
     "button was answered twice in development. Now the scheduler remembers the name and typed email for the "
     "whole conversation and fills them into any new proposal (Change email clears it, so it is asked again); "
     "anything that isn't a real address is ignored; Nofa is told the name and that the email is saved and to "
     "leave it out. The chat fetches the next card while the reply is written and shows both together. The "
     "\"what's on screen\" note is given only on the website (chat, web call), never on phone or WhatsApp. "
     "These rules live in shared ZOVAK code, so website chat, website calls, phone calls and WhatsApp all "
     "follow them. Email box hint: \"Nothing is booked until you confirm.\""),
    ("2026-10-30", "Change the time from the review card; name and email are kept",
     "Bug: with the review card showing, \"can I change the time\" kept the review card, and saying \"5 pm\" "
     "made a new proposal that asked for the email again. Now a change-the-time request (typos like \"chane\" "
     "count; \"check the time\" doesn't) sets the waiting proposal aside, keeps the same doctor chosen and "
     "brings the time buttons back (DemoScheduler.reopen_times). A new proposal in the same conversation reuses "
     "the email already typed, and the name is passed to Nofa as already known, so the next step is straight "
     "to Review your appointment. On website calls an email typed on screen earlier in the call is trusted for "
     "the new time (EmailFormGate.arm(trusted_email)); Change email, or saying the email is wrong, still asks "
     "again."),
    ("2026-10-30", "Naming a doctor directly shows their times; changing reopens the choice",
     "Bug: \"book with Dr. Farhan Karim for a skin rash\" showed no doctor card and no times, while Nofa said "
     "\"pick a time below\". A choice only counted when picked from offered cards, and Nofa re-offering the "
     "same doctor wiped it. Now: naming exactly one clinic doctor chooses them even with no cards (and Nofa "
     "checking one doctor's times counts too); offering an already-chosen doctor keeps the choice; \"change "
     "the time\" / \"another slot\" brings the time buttons back and \"a different doctor\" starts the doctor "
     "choice again (read_choices, plain code before the AI). Nofa's live status now says what the website "
     "shows (WEBSITE SCREEN: doctor chosen, buttons showing or time picked), and she may only point at "
     "buttons that are really there."),
    ("2026-10-30", "Time buttons always visible; Nofa doesn't list times on the website",
     "Bug: Nofa typed out every free time, pushing the time buttons below the bottom of the chat, and the chat "
     "didn't scroll to new cards. Now any new or changed card (doctor cards, time buttons, booking card) is "
     "scrolled into view, and on the website Nofa never lists the times: she mentions at most the earliest one "
     "and points to the buttons, asking only for what's still missing."),
    ("2026-10-30", "Time buttons collapse once a time is picked",
     "Bug: after tapping a time the full list of buttons stayed under every reply. Now plain code recognises a "
     "tapped time in the patient's message (pick_tapped_time, only if it is one of the chosen doctor's free "
     "times) and ZOVAK remembers it; the card collapses to \"Time selected · Fri, Oct 30 · 11:00 AM with Dr. …\" "
     "with a Change time link that brings the buttons back. The picked time is forgotten when another doctor "
     "or kind of care is chosen, and when the booking is prepared."),
    ("2026-10-30", "Free times as tap buttons on the website",
     "After a doctor is chosen, the \"Doctor selected\" card lists that doctor's real free times for the next "
     "3 days that have any (day tabs + time buttons): only starts inside their hours, not lunch, not booked, "
     "not past and not held by another patient (free_days in booking_state.py; none if the calendar can't be "
     "read, and typing still works). Chat: a tap sends \"I'd like Friday, October 30 at 11:30 AM with Dr. …\" "
     "as the patient's message. Website call: a tap goes to /api/v1/dev/voice/pick-slot, which accepts only one "
     "of those free times and runs it through the call exactly like speech (VoiceSideband.pick_slot), so Nofa "
     "answers aloud. A tap only proposes; confirming is unchanged. Chat now proposes as soon as doctor, time and "
     "name are known and leaves the email to the card's email box. Phone calls are unchanged (no screen)."),
    ("2026-10-29", "Website: full-page light ribbons and light/dark themes (redesign branch)",
     "On redesign/animated: the light ribbons are one fixed background behind every section; they drift and "
     "tilt with scroll position and swell with scroll speed. A sun/moon switch in the header (and in the staff "
     "portal) toggles light and dark; the choice is remembered, dark is the default, and a small script in "
     "<head> applies it before the page paints. All colours are CSS variables (globals.css), so the homepage, "
     "the chat/voice widget and the staff portal switch together; in dark the widget's accents become the "
     "homepage's aqua with dark text on bright buttons. No backend changes."),
    ("2026-10-29", "Website: animated \"night shift\" homepage on a separate branch",
     "The website as it was is committed and pushed to GitHub main (3c378ad). A new animated homepage "
     "lives on the branch redesign/animated (6cba4d0), so it can be dropped with git checkout main. Inspired "
     "by the reference videos: dark canvas with flowing light ribbons (canvas 2D, additive strands, paused "
     "off-screen, still under reduced motion), headlines that rise word by word with serif italic accents, "
     "a pinned scroll story of one booking (listen → right doctor → free time → confirm and goodbye), a "
     "scroll-lit statement of the one rule, channel cards with live mini demos, an interactive running-late "
     "planner that uses the same ripple rule as RecoveryPlanner.preview, an animated staff desk and a bright "
     "closing panel. Files: serena-health/src/components/night/*, src/app/page.tsx. No backend changes."),
    ("2026-10-29", "Staff assistant: a fresh memory per sign-in",
     "Bug: the staff assistant's memory outlived backend restarts and page reloads, while the chat on "
     "screen started empty and old delay plans were gone, so it mentioned plans that no longer existed "
     "(\"Your 1-hour delay plan hasn't been applied\"). Now each sign-in gets its own memory (a random "
     "id, never the sign-in token), the on-screen chat is kept in the tab while signed in and cleared on "
     "sign-in/sign-out, and the assistant answers only what was asked. Note: if the website's dev server "
     "stops rebuilding after edits, restart npm run dev."),
    ("2026-10-29", "Delays count from the right moment; staff chat scrolls and streams",
     "Bug: a delay always counted from \"now\" and only looked at the rest of today. At 10 PM Dr. Karim "
     "said he'd be 30 minutes late; his day had ended, so nobody was found, and the assistant offered "
     "to \"apply\" an empty plan. Now the delay counts from now while the doctor is working, from their "
     "start time before their day begins, and from the next day's start after it ends. Staff can also "
     "give a time (\"30 minutes late for my 1:00\"): the Running late page has \"Late from: Now / A "
     "specific time\", and the assistant passes late_from. Every preview says when it counts from; when "
     "nobody is affected it says why (\"first patient is Chandni at 11:30; the delay ends at 11:30, in "
     "time for them\") and there is nothing to send. The assistant re-checks on every report instead of "
     "reusing an old answer from earlier in the chat. The staff assistant chat now keeps the newest "
     "message in view and writes replies out word by word with a typing indicator."),
    ("2026-10-29", "Doctors can see any day in their schedule",
     "The staff Schedule tab had only Today and Tomorrow, so a booking for next week was saved but "
     "invisible in the doctor's login. Now there are ‹ › buttons to step a day at a time and a date "
     "picker for any day. Bookings from website chat, website voice and phone all land in the same "
     "booking store the portal reads (refreshed every 5 s); doctors have no calendars of their own."),
    ("2026-10-29", "Warm automatic goodbyes, matching doctors only, compact call list, new greeting",
     "Goodbyes: plain code now checks everything the caller says, not only what the voice model "
     "passes on (it often answered \"bye\" itself, so the call stayed open). A clear goodbye (\"thanks, "
     "bye\", \"I have to go\", \"hang up\"; not \"okay\" alone, not \"bye, but first…\") ends the call "
     "with a personal message (\"You're all set with Dr. Rahman tomorrow at 11:30 AM. Thanks for calling "
     "Brightside Family Clinic. Take care, goodbye!\"). If a booking or change is half-done, Nofa asks "
     "once (\"Before you go, your appointment isn't booked yet…\"); a second goodbye ends it with nothing "
     "booked or changed. Doctors: offer_doctors refuses doctors from more than one specialty, so a list "
     "of every doctor is never shown or read out; asked \"which doctors do you have?\", Nofa describes "
     "the kinds of care and asks about the problem first. Website calls show the matching doctors as a "
     "slim scrolling list so the transcript stays visible. New chat greeting and buttons."),
    ("2026-10-29", "Doctor cards go away once a doctor is picked",
     "Bug: the doctor cards stayed under every message until the whole booking was prepared, "
     "so the patient kept seeing \"Choose\" after choosing. Now plain code (not the AI) checks "
     "each chat message and spoken turn: if it names exactly one offered doctor (tapping Choose, "
     "typing or saying the name; close speech spellings count; \"not Rahman\" or naming both "
     "doesn't), the cards are replaced by a \"Doctor selected\" card that says what happens next. "
     "Nofa checking one offered doctor's times also counts as a pick, and she doesn't re-offer "
     "the list unless asked. Booked/cancelled cards are no longer hidden by new doctor cards."),
    ("2026-10-29", "Manual test plan",
     "New docs/ZOVAK_Test_Plan.pdf (built by docs/build_test_plan.py): numbered hands-on test cases "
     "with Pass/Fail boxes for website chat, website voice, phone calls and the SMS form, the staff "
     "portal (sign-in, running late, patient choices), the staff assistant, an end-to-end story and "
     "security checks. WhatsApp tests are paused while the Meta account is under review."),
    ("2026-10-29", "WhatsApp typing indicator shown once",
     "After Meta restricted the business account (error 131031, automated account-integrity "
     "review), the typing indicator is sent once per message instead of refreshed every 20 s, "
     "keeping API traffic human-paced. No Meta API calls were ever made by the tests."),
    ("2026-10-28", "Delay recovery engine + staff portal (phases 2 and 3)",
     "A doctor's delay ripples forward only until free time absorbs it; each affected patient gets "
     "options (same time with another free doctor, the shifted time, the doctor's next free times "
     "after they arrive, cancel). Staff preview first; sending applies shifted times latest-first and "
     "notifies patients with a one-time /update link. New /staff portal: sign-in, schedule per "
     "doctor, Running late with preview, patient updates, staff assistant. Demo bookings are now "
     "created around the current time, and the two GPs are staggered."),
    ("2026-10-28", "Doctors and staff (phase 1 of schedule recovery)",
     "Four demo doctors (General Practice ×2, Pediatrics, Dermatology) with their own services, "
     "hours and lunch breaks; two officials; demo bookings for today/tomorrow loaded at startup. "
     "Every booking belongs to a doctor; availability, holds and overlaps are per doctor. Nofa "
     "first asks what kind of doctor the patient needs, then offers matching doctors (cards on "
     "the website, names on calls) or says the clinic doesn't offer it."),
    ("2026-10-28", "WhatsApp calls share the person's WhatsApp chat conversation",
     "Calls now use the same conversation as that number's chat (wa-<number>), so a booking made "
     "in chat is known on a call and vice versa, with shared agent memory. Safe because Meta "
     "verifies WhatsApp numbers (unlike phone caller ID). Phone and website still have their own "
     "conversations until customer tracking is built."),
    ("2026-10-28", "WhatsApp calls work through the audio bridge; decision record added",
     "A live WhatsApp call to the test number was answered by Nofa through the bridge. Added a "
     "decision record explaining every route tried, why each failed or was chosen, and where "
     "the code is."),
    ("2026-10-28", "WhatsApp calls through ZOVAK's audio bridge",
     "Diagnostics confirmed Meta and GPT-Live are both ICE-lite (a=ice-lite in both SDPs), so "
     "neither starts the connection and a direct call never gets audio. New AudioBridge (aiortc) "
     "answers Meta's offer and calls GPT-Live like a browser, relaying audio both ways at real "
     "time (silence until audio arrives, one fixed mono 48 kHz format, backlog capped). Verified "
     "locally with two real WebRTC peers: tones flowed both ways."),
    ("2026-10-28", "WhatsApp call experiment: no audio; SDP diagnostics",
     "The first WhatsApp test call was accepted but no audio flowed and Meta ended it as FAILED "
     "after ~21 s. ZOVAK now logs the connection-relevant SDP lines of Meta's offer and GPT-Live's "
     "answer (ice-lite, setup role, candidates) to confirm why."),
    ("2026-10-28", "WhatsApp working: subscribe the account to the app; typing indicator",
     "Messages showed in Meta's 'Check test webhooks' but never reached ZOVAK: the test flow had "
     "subscribed the WhatsApp account only to Meta's test viewer app. POST /{WABA_ID}/subscribed_apps "
     "with the app's token fixed it. Nofa now marks messages read and shows 'typing…' (refreshed every "
     "20 s) while the agent works."),
    ("2026-10-28", "WhatsApp chatbot + WhatsApp calling experiment",
     "Nofa answers WhatsApp messages (Meta Cloud API, free test number): same agent, email typed "
     "in chat, tap buttons for Confirm booking / Confirm move / Cancel / Keep, signed webhooks, "
     "rate and change limits. Experimental WhatsApp calls: Meta's WebRTC offer is handed to "
     "GPT-Live and its answer returned to Meta, with the phone call brain attached."),
    ("2026-10-28", "Correction: Sweden SMS was not delivered",
     "Telnyx accepted the Sweden test text (queued) but the Swedish carrier rejected it "
     "(40008 Undeliverable), most likely the unregistered NofaClinic sender. Real SMS through "
     "Telnyx is unreliable without a registered sender; use ntfy for testing."),
    ("2026-10-28", "SMS router: test inbox, ntfy push, Telnyx by country",
     "Every text goes through SmsRouter.send(): always to the test inbox; to the ntfy app on "
     "your phone when TEST_SMS_NTFY_TOPIC is set (free testing); and to a real provider chosen "
     "by country code. Telnyx is wired for non-Bangladeshi numbers; +880 is skipped because Telnyx requires a registered "
     "sender there. A Bangladeshi gateway plugs in later as one adapter."),
    ("2026-10-27", "Noise guardrail; how calls connect, in detail",
     "Coughs, sniffs, 'uh'/'hmm' and transcription tags like [cough] no longer reset the silence "
     "timer. A turn with only noise isn't sent to the AI; right after the 10 s warning (someone "
     "pretending to answer), or on the 3rd noise-only turn, Nofa ends the call politely. The "
     "documentation now walks through how WebRTC and SIP calls connect to GPT-Live, with code."),
    ("2026-10-27", "First successful phone booking through Telnyx",
     "Linphone direct to OpenAI connected but no audio ever flowed (no packets, nothing billed). "
     "A Telnyx US number with an FQDN SIP connection to OpenAI (TLS + SRTP) worked end to end: "
     "Nofa answered, the booking was prepared, 'confirm' sent the form, and the form booked it."),
    ("2026-10-26", "Phone call diagnostics",
     "First Linphone test: the call was accepted but no audio was heard, and the logs couldn't show "
     "why. ZOVAK now prints its own timestamped log lines (caller said / Nofa replies / caller audio "
     "arriving / Nofa speaking), logs whether the call audio is encrypted (SRTP) or plain, greets "
     "phone callers first, and only starts the silence timer once caller audio has arrived. If no "
     "caller audio arrives within 45 s it ends the call as 'no_audio' and says so in the log."),
    ("2026-10-26", "Phone calls (Linphone/SIP), SMS booking form, safer natural yes, call guardrails",
     "Phone calls reach ZOVAK through GPT-Live SIP (tested free with Linphone). On a call, saying "
     "'confirm' texts a one-time 2-minute form; the form is what books (edit name, add email, pick "
     "another time, or cancel). Spoken yes accepts natural wording but never a reply that could be "
     "the start of 'yes, but…' (action word required, hedge words block it, and ZOVAK waits for "
     "silence first). Calls end after 10 s silence + a 10 s warning, when the caller says that's "
     "all, or when someone keeps booking/moving/cancelling. Pending proposals now hold their slot."),
    ("2026-10-24", "Agent always knows the real booking status",
     "Bug: after confirming with the on-screen button, the agent still believed the booking was "
     "pending (its memory only had the 'PENDING' tool result) and refused to move it. Fix: every "
     "agent turn now gets the live booking status from the scheduler, and every button or strict "
     "spoken confirmation adds a note to the agent's memory."),
    ("2026-10-24", "Widget polish, call timer, card/transcript overlap fix, this documentation",
     "Widget font changed to Figtree; chat bubbles restyled; the voice panel shows a compact "
     "header with a live call timer during a call; the booking card can no longer be squeezed "
     "over the transcript. Documentation PDF added and rebuilt after every change."),
    ("2026-10-24", "Call ends on silence or off-topic requests",
     "CallGuard counts caller silence (30 s, 2 min while the email box is open) and off-topic "
     "strikes (3). Nofa checks in, says goodbye, and the website hangs up."),
    ("2026-10-24", "Booking card, move, cancel, reminders, today's date",
     "Shared booking-state builder for chat and voice; Confirm booking button; same-conversation "
     "move and cancel with Google Calendar update/delete; 1-hour Resend reminders; the agent and "
     "GPT-Live are told today's date so Nofa never asks for the year."),
    ("2026-10-23", "Typed email box for voice calls",
     "Callers type their email on screen instead of spelling it aloud; typing never books."),
    ("2026-10-23", "Website connected to ZOVAK",
     "The Nofa website's chat and voice call talk to the local ZOVAK backend through Next.js "
     "routes that forward requests server-side."),
]


# ── The document ─────────────────────────────────────────────────────────────

def content() -> str:
    s = []
    add = s.append

    add(f"""
<header class="cover">
  <p class="kicker">ZOVAK · Nofa Health</p>
  <h1>How the AI receptionist works</h1>
  <p class="sub">Chat and voice booking, the booking card, moving and cancelling,
  reminders and call rules — step by step, with the real code.</p>
  <p class="meta">Built {date.today().strftime('%B %d, %Y')} from the current source of
  <code>zovak-backend</code> and <code>serena-health</code>.</p>
</header>

<nav class="toc"><h2>Contents</h2><ol>
<li>The big picture</li><li>Running it</li><li>The safety rules</li>
<li>Booking states and the card</li><li>How the card is triggered: chat</li>
<li>How the card is triggered: voice</li><li>Booking a new appointment</li>
<li>Moving an appointment</li><li>Cancelling an appointment</li>
<li>Spoken confirmations</li><li>Reminder emails</li><li>Today's date and live booking status</li>
<li>Ending a call and guardrails</li><li>Phone calls (SIP, Telnyx)</li>
<li>The SMS booking form</li><li>WhatsApp chat and calls</li><li>Doctors and staff</li><li>Delay recovery and the staff portal</li><li>The widget UI</li>
<li>File map</li><li>Limits and what's next</li><li>Why it's built this way</li>
<li>Change log</li></ol></nav>
""")

    add("""
<h2>1. The big picture</h2>
<p>Two projects work together. <b>serena-health</b> is the Nofa Health website (Next.js); its
chat and voice widget is the customer's screen. <b>zovak-backend</b> (Python, FastAPI) is ZOVAK,
the receptionist's brain: the AI agent, the scheduler and Google Calendar.</p>
""" + boxes(
        ("Customer's browser", "Nofa widget: chat bubbles, voice call, booking card"),
        ("Next.js routes (serena-health)", "/api/agent/chat · /api/agent/session · /api/agent/booking — forward to ZOVAK on the server, so the browser never talks to ZOVAK directly"),
        ("ZOVAK FastAPI (zovak-backend)", "Dev routes for chat, voice routes protected by a per-call token"),
        ("Agent + Scheduler", "OpenAI Agents SDK receptionist proposes; the scheduler checks rules and commits"),
        ("Google Calendar · Resend · GPT-Live", "Events with email invites · reminder emails · live voice"),
    ) + """
<p><b>Voice audio</b> does not go through ZOVAK: the browser streams audio straight to OpenAI's
GPT-Live over WebRTC. ZOVAK attaches a <i>sideband</i> WebSocket to the same live session: it
hears the caller's transcript, runs the agent when GPT-Live delegates a request, and tells GPT-Live
what to say.</p>
""" + ts("src/lib/agent/zovak.ts", "export async function zovakFetch") + """
<p>Every Next.js route uses <code>zovakFetch</code>. <code>ZOVAK_API_URL</code> in
<code>.env.local</code> points at the backend (<code>http://127.0.0.1:8000</code> locally).</p>
""")

    add("""
<h2>2. Running it</h2>
""" + table(("What", "Command / setting"),
            ("ZOVAK backend", "<code>uvicorn zovak.main:app --reload --host 127.0.0.1 --port 8000</code> (conda env <code>Crewai</code>)"),
            ("Website", "<code>npm run dev</code> in serena-health, then open <code>http://localhost:3000</code>"),
            ("Website → ZOVAK", "<code>.env.local</code>: <code>ZOVAK_API_URL</code>, <code>NEXT_PUBLIC_AGENT_LIVE=true</code>"),
            ("Google Calendar", "Open <code>/api/v1/auth/google/start</code> after every backend restart (tokens live in memory)"),
            ("Reminders", "<code>.env</code>: <code>RESEND_API_KEY</code>, <code>REMINDER_FROM_EMAIL</code> (verified domain)"),
            ("Tests", "<code>python -m pytest -q</code> in zovak-backend; <code>npx tsc --noEmit</code> in serena-health"),
            ("Deploy (Vercel + ngrok)", "zovak-backend/.env: PUBLIC_BASE_URL=https://stapling-delete-stunner.ngrok-free.dev and SITE_API_KEY=&lt;random&gt;; run ngrok on port 8000 with that domain. Vercel env: ZOVAK_API_URL (same ngrok URL, or edit src/config/backend.ts), ZOVAK_SITE_KEY (same random value), DEMO_ACCESS_CODE, NEXT_PUBLIC_AGENT_LIVE=true")) + """
<p>On start-up ZOVAK creates the Google-backed scheduler and, if Resend is configured, starts the
reminder job:</p>
""" + py("zovak/main.py", "lifespan"))

    add("""
<h2>3. The safety rules</h2>
<ul>
<li><b>The AI proposes, the customer confirms.</b> Agent tools only create <i>pending</i> items
(a booking proposal, a move, a cancellation). Nothing is booked, moved or cancelled until the
customer taps a button or clearly says yes.</li>
<li><b>IDs come from the server.</b> Buttons send the ID of the single pending item; ZOVAK checks
it matches what it holds for that conversation, so a stale or guessed ID is rejected.</li>
<li><b>Never say "booked" before the calendar confirms.</b> Messages are built from verified
results, not the model's text.</li>
<li><b>Typed email, never spelled.</b> Saving an email never books anything.</li>
<li><b>Voice actions need the call's secret token</b> (<code>ui_token</code>), created fresh for
every call.</li>
</ul>
""")

    add("""
<h2>4. Booking states and the card</h2>
<p>Both chat and voice ask ZOVAK one question: <i>what should the card show now?</i> The answer is a
<code>stage</code>, computed from the scheduler's records by one shared function.</p>
""" + table(("Stage", "Meaning", "Card shows"),
            ("<code>email_required</code>", "Agent prepared an appointment, no email yet", "Email box"),
            ("<code>ready_to_confirm</code>", "Pending appointment with email", "Details + Confirm booking + Change email"),
            ("<code>booked</code>", "Confirmed booking in this conversation", "Green card"),
            ("<code>move_pending</code>", "Agent proposed a new time", "Old → new time + Confirm move / Keep"),
            ("<code>cancel_pending</code>", "Agent proposed cancelling", "Cancel appointment / Keep it"),
            ("<code>cancelled</code>", "Booking was cancelled", "Grey card"),
            ("<code>idle</code>", "Nothing yet", "No card")) +
        py("zovak/services/booking_state.py", "booking_state") + """
<p>The data behind it: a <code>BookingProposal</code> (pending or confirmed booking) and a
<code>ChangeRequest</code> (pending move/cancel):</p>
""" + py("zovak/domain/schemas.py", "ChangeRequest"))

    add("""
<h2>5. How the card is triggered: chat</h2>
""" + flow(
        "Customer types a message. The widget calls <code>sendChatMessage</code> → <code>/api/agent/chat</code> → ZOVAK <code>/api/v1/dev/chat</code>.",
        "ZOVAK runs the receptionist agent. If details are complete it calls the <code>propose_booking</code> tool, which stores a <i>pending</i> proposal.",
        "The reply streams into the chat word by word.",
        "Right after the reply, the widget asks for the card: <code>fetchBookingState(getChatChannel())</code> → <code>/api/agent/booking?channel=chat</code> → ZOVAK <code>/api/v1/dev/booking-state</code>.",
        "ZOVAK answers with the stage (e.g. <code>ready_to_confirm</code>) and the widget renders <code>BookingPanel</code> under the messages.",
        "A button tap calls <code>sendBookingAction</code>, then fetches the state again so the card updates instantly.",
    ) + ts("src/components/assistant/AssistantWidget.tsx", "const send = useCallback(", label="ChatView.send") +
        ts("src/lib/agent/client.ts", "export async function fetchBookingState") +
        py("zovak/api/routes/dev.py", "chat_booking_state"))

    add("""
<h2>6. How the card is triggered: voice</h2>
""" + flow(
        "Customer taps Start call. The browser makes a WebRTC offer and sends it via <code>/api/agent/session</code> to ZOVAK <code>/api/v1/dev/voice/session</code>.",
        "ZOVAK creates the GPT-Live session (with today's date in the instructions), attaches its sideband and returns the answer, the <code>conversation_id</code> and a fresh <code>ui_token</code>.",
        "Audio flows browser ↔ OpenAI. When the caller asks for something, GPT-Live <i>delegates</i>; the sideband runs <code>_handle_turn</code>, which runs the agent and returns what Nofa should say.",
        "Meanwhile the browser polls the card every 1.5 seconds: <code>pollBooking</code> → <code>/api/agent/booking?channel=voice</code> (with the token) → ZOVAK <code>/voice/booking-state</code>.",
        "As soon as a proposal, move or cancel exists, the card appears in the voice panel. A tap runs the action, and the sideband also <i>speaks</i> the verified result on the call.",
    ) + ts("src/lib/agent/client.ts", "const pollBooking = async", label="startLiveVoiceSession → pollBooking") +
        py("zovak/voice/sideband.py", "VoiceSideband.booking_state") +
        py("zovak/api/routes/voice_dev.py", "_get_authorized_voice") + """
<h3>6.1 Under the hood: how a website call connects to GPT-Live (WebRTC)</h3>
<p>Three separate connections make up one call. Only the first carries sound:</p>
""" + table(("Connection", "Between", "Carries"),
            ("<b>WebRTC audio</b> + a small data channel", "Browser ↔ OpenAI", "Your voice, Nofa's voice; events like <code>session.started</code> and live transcripts"),
            ("<b>HTTPS</b> (one request)", "Browser → Next.js → ZOVAK → OpenAI", "The WebRTC offer/answer that sets up the audio"),
            ("<b>Sideband WebSocket</b>", "ZOVAK ↔ OpenAI", "Control only: transcripts in, delegated requests in, Nofa's verified replies out")) + flow(
        "Start call: the browser opens the microphone, creates an <code>RTCPeerConnection</code> and a data channel, and writes an <b>SDP offer</b> (a description of how it can send and receive audio).",
        "The offer goes to <code>/api/agent/session</code> (Next.js) → ZOVAK <code>/api/v1/dev/voice/session</code>. ZOVAK holds the OpenAI key, so the browser never sees it.",
        "ZOVAK calls <code>client.live.create(...)</code> with the offer, Nofa's instructions (plus today's date) and <code>delegation: client</code>. OpenAI returns an <b>SDP answer</b> and a session ID.",
        "ZOVAK attaches its <b>sideband</b> to that session (<code>wss://api.openai.com/v1/live/sessions/{id}/attach</code>), then returns the answer, a conversation ID and a per-call <code>ui_token</code>.",
        "The browser applies the answer: audio now flows directly browser ↔ OpenAI. OpenAI sends <code>session.started</code> on the data channel → the widget switches to \"Listening…\".",
        "When you ask for something, GPT-Live <b>delegates</b>: the sideband receives <code>session.delegation.created</code>, ZOVAK runs the agent and booking rules, and sends the reply back as <code>session.commentary.append</code>; Nofa says it.",
    ) + ts("src/lib/agent/client.ts", "function startLiveVoiceSession(", max_lines=95) +
        py("zovak/api/routes/voice_dev.py", "create_voice_session") +
        py("zovak/voice/sideband.py", "VoiceSideband._listen") +
        py("zovak/voice/sideband.py", "VoiceSideband._delegate", max_lines=45))

    add("""
<h2>7. Booking a new appointment</h2>
<h3>7.1 The agent proposes</h3>
""" + py("zovak/agents/receptionist.py", "propose_booking") + """
<p>In voice calls <code>email_via_form</code> is on, so any spoken email is dropped: the email must
be typed in the box. The scheduler checks the slot is really free and keeps only one pending
proposal per conversation:</p>
""" + py("zovak/services/mock_scheduler.py", "DemoScheduler.propose", max_lines=40) + """
<h3>7.2 The email box</h3>
<p>The card shows the email box while the stage is <code>email_required</code>. Submitting goes
through the shared action route; in voice it reaches <code>submit_email</code>, which validates the
address, attaches it to the pending proposal and reads the details back — but never books.</p>
""" + py("zovak/voice/sideband.py", "VoiceSideband.submit_email", max_lines=45) + """
<h3>7.3 Every card button goes through one route</h3>
<p><code>/api/agent/booking</code> maps each button to the right ZOVAK endpoint for chat or voice:</p>
""" + ts("src/app/api/agent/booking/route.ts", "const ACTIONS: Record<BookingAction") + """
<h3>7.4 Confirm booking</h3>
""" + py("zovak/voice/sideband.py", "VoiceSideband.confirm_booking") +
        py("zovak/voice/sideband.py", "VoiceSideband._commit_booking", max_lines=20) + """
<p>With Google connected, confirming creates the calendar event with a deterministic ID (so a retry
after a network error can't create a duplicate) and asks Google to email the invitation:</p>
""" + py("zovak/services/google_scheduler.py", "GoogleBackedScheduler.confirm", max_lines=48))

    add("""
<h2>8. Moving an appointment</h2>
""" + flow(
        "Customer: \"Can we move it to 4:30?\" The agent checks availability and calls <code>propose_move</code>.",
        "The scheduler stores a pending <code>ChangeRequest(kind='move')</code>; the booking itself is untouched.",
        "The card switches to <code>move_pending</code> (old time crossed out → new time). On a call, Nofa also asks: \"Tap Confirm move, or say 'Yes, confirm the move.'\"",
        "Confirm move → <code>apply_change</code>: re-checks the slot, moves the Google event (PATCH, Google emails the update), then updates the booking and resets its reminder.",
    ) + py("zovak/agents/receptionist.py", "propose_move") +
        py("zovak/services/mock_scheduler.py", "DemoScheduler.propose_move") +
        py("zovak/services/mock_scheduler.py", "DemoScheduler.apply_change") +
        py("zovak/services/google_scheduler.py", "GoogleBackedScheduler._calendar_apply") +
        py("zovak/providers/calendar/google_invites.py", "GoogleInvites.move_event") + """
<p>When checking the new time, the booking's <i>own</i> slot counts as free
(<code>ignore_booking_id</code>), otherwise a small shift like 2:00 → 2:30 would clash with itself.</p>
""")

    add("""
<h2>9. Cancelling an appointment</h2>
""" + flow(
        "Customer: \"Please cancel my appointment.\" The agent calls <code>propose_cancellation</code>.",
        "The card shows <code>cancel_pending</code> with Cancel appointment / Keep it.",
        "Cancel appointment → <code>apply_change</code> deletes the Google event (Google emails a cancellation), removes the booking and frees the slot. Keep it → <code>discard_change</code>; nothing changes.",
    ) + py("zovak/agents/receptionist.py", "propose_cancellation") +
        py("zovak/services/mock_scheduler.py", "DemoScheduler.propose_cancel") +
        py("zovak/providers/calendar/google_invites.py", "GoogleInvites.cancel_event") +
        py("zovak/voice/sideband.py", "VoiceSideband._commit_change"))

    add("""
<h2>10. Spoken confirmations</h2>
<p>Speech arrives in pieces, so a bare "yes" might be the start of "yes, but change the time". The
rules run in plain code, not the AI, and accept natural wording ("uh, yes, confirm it now please",
"go ahead", "okay book it") while never treating a possibly-unfinished reply as a yes:</p>
""" + flow(
        "<b>An action is required.</b> \"Yes\" or \"sure\" alone is not enough; there must be \"confirm\", \"book\", \"go ahead\", \"do it\"…",
        "<b>Hedge or change words block it:</b> but, wait, hold, change, actually, not, maybe, tomorrow, pm…",
        "<b>Unfinished replies don't count:</b> one ending in \"my\", \"the\", \"to\"… (\"yes, confirm my\") is cut off.",
        "<b>ZOVAK waits 1.5 s of silence</b> before acting. If the caller keeps talking, nothing happens; their next words are handled as a new turn.",
    ) + py("zovak/voice/confirmation.py", "_is_approval") +
        py("zovak/voice/confirmation.py", "classify_confirmation") +
        py("zovak/voice/sideband.py", "VoiceSideband._settled") +
        py("zovak/voice/confirmation.py", "classify_change_reply") + """
<p>Every spoken turn goes through <code>_handle_turn</code>. It starts by looking up the trusted
pending proposal for this call:</p>
""" + py("zovak/voice/sideband.py", "VoiceSideband._handle_turn", max_lines=12) + """
<p>It then, in order: handles a pending proposal (email gate, approve, decline, revise); handles a
pending move/cancel with <code>classify_change_reply</code>; otherwise runs the agent, applies the
off-topic check, and announces any newly proposed booking or change.</p>
""")

    add("""
<h2>11. Reminder emails</h2>
<p>A background task checks every minute for confirmed bookings starting within the next hour and
emails the customer once through Resend. It skips bookings made inside that hour, follows moved
bookings (their reminder flag is reset) and never reminds cancelled ones. An idempotency key makes a
retried send deliver only once.</p>
""" + py("zovak/workers/reminders.py", "ReminderWorker.run_once") +
        py("zovak/providers/notifications/resend_email.py", "send_resend_email"))

    add("""
<h2>12. Today's date and live booking status</h2>
<p>The agent and GPT-Live are given today's date and time (Dhaka) on every chat turn and call, so
Nofa resolves "tomorrow" or "26th October" itself and never asks for the year.</p>
""" + py("zovak/agents/receptionist.py", "today_instructions") + """
<h3>12.1 Why the agent needs the live status</h3>
<p>Buttons and strict spoken confirmations change bookings <i>without</i> the agent. Its memory
would still hold the old tool result ("PENDING… not confirmed"), so after a button confirmation it
once refused to move a booking that was already confirmed. Two safeguards now prevent this:</p>
""" + flow(
        "Before every agent turn, <code>booking_status_for_agent</code> reads the scheduler and the status is put into the agent's instructions, marked as always current.",
        "Whenever a booking is confirmed, moved, cancelled or kept outside the agent, <code>note_for_agent</code> adds a short system note to the agent's memory.",
    ) + py("zovak/services/booking_state.py", "booking_status_for_agent") +
        py("zovak/agents/memory.py", "note_for_agent"))

    add("""
<h2>13. Ending a call and guardrails</h2>
<p>The same rules apply to website and phone calls. <code>CallGuard</code> is plain bookkeeping the
sideband feeds with events.</p>
""" + table(("When", "What Nofa does"),
            ("10 s of silence", "Warns: \"If I don't hear anything in the next 10 seconds, I'll end the call, and you won't be able to say anything after that.\""),
            ("10 s more of silence", "Says goodbye and ends the call (website: 2 minutes while the email box is open)"),
            ("An action finished", "Asks \"Is there anything else?\"; \"no, that's all\" or \"bye\" ends the call"),
            ("The caller says goodbye (any time)", "\"Thanks, bye\", \"I have to go\", \"you can hang up\"… Nofa ends the call with a personal goodbye (the booked doctor, day and time, if any). Checked on every caller turn, even ones the voice model answers itself"),
            ("Goodbye with a booking half-done", "Asks once: \"Before you go, your appointment isn't booked yet. Would you like to confirm it, or leave it for now?\" A second goodbye ends the call; nothing is booked (a pending move/cancel is dropped too)"),
            ("Off-topic requests", "Refuses politely; the 3rd ends the call"),
            ("Booking, moving, cancelling over and over", "More than 4 prepared actions in one call, or the agent's <code>report_misuse</code>, ends the call at once"),
            ("Too many calls from one caller", "More than 5 calls an hour from the same caller ID are rejected (busy)"),
            ("Only noise (cough, sniff, \"uh\", \"hmm\", [cough])", "Doesn't reset the silence timer and isn't sent to the AI. Right after the warning, or on the 3rd noise-only turn, Nofa ends the call politely"),
            ("Goodbye started", "Nothing the caller says counts any more: the website mutes the mic, the backend ignores speech")) +
        py("zovak/voice/call_guard.py", "CallGuard.tick") +
        py("zovak/voice/call_guard.py", "CallGuard.count_action") +
        py("zovak/voice/confirmation.py", "is_meaningful") +
        py("zovak/voice/sideband.py", "VoiceSideband._caller_said") +
        py("zovak/voice/sideband.py", "VoiceSideband._noise_only_turn") +
        py("zovak/voice/sideband.py", "VoiceSideband._end") +
        py("zovak/voice/sideband.py", "VoiceSideband._finish_call") +
        py("zovak/voice/confirmation.py", "is_goodbye") +
        py("zovak/voice/sideband.py", "VoiceSideband._goodbye_turn") +
        py("zovak/voice/sideband.py", "VoiceSideband._farewell") +
        py("zovak/voice/sideband.py", "VoiceSideband._check_goodbye") +
        py("zovak/agents/receptionist.py", "report_misuse") + """
<p>The website sees <code>call_ended</code> (or <code>ending</code>) in the polled state, mutes
the mic and hangs up:</p>
""" + ts("src/lib/agent/client.ts", "const endedRemotely = ", label="endedRemotely"))

    add("""
<h2>14. Phone calls (SIP, Telnyx)</h2>
<p>A phone call reaches OpenAI GPT-Live over SIP. OpenAI asks ZOVAK to accept it; after that ZOVAK
runs the call with the same sideband as the website. Nothing in ZOVAK depends on who carries the
call, so the Telnyx test number now and a real Bangladeshi number later use the same code.</p>
<h3>Under the hood: how a phone call connects</h3>
""" + table(("Connection", "Between", "Carries"),
            ("<b>Phone network</b>", "Caller's phone ↔ Telnyx", "The normal phone call"),
            ("<b>SIP over TLS</b> (signaling)", "Telnyx → OpenAI (<code>sip:proj_…@sip.api.openai.com:5061</code>)", "\"Here's a call for project proj_…\" — the Translated Number puts the project ID there"),
            ("<b>SRTP</b> (encrypted audio)", "Telnyx ↔ OpenAI", "The voices. Telnyx's public servers make this work; a phone app behind a router (Linphone) couldn't"),
            ("<b>Signed webhook</b> (HTTPS)", "OpenAI → ZOVAK (through ngrok)", "<code>live.transport.incoming</code>: session ID, SIP headers (caller ID), audio security"),
            ("<b>Accept</b> (HTTPS)", "ZOVAK → OpenAI", "<code>POST /v1/live/sessions/{id}/accept</code> with Nofa's phone instructions"),
            ("<b>Sideband WebSocket</b>", "ZOVAK ↔ OpenAI", "Same as the website: transcripts, delegation, replies, hang-up")) + flow(
        "The caller dials the Telnyx number. Telnyx sends a SIP INVITE over TLS to OpenAI, with the user part translated to your <code>proj_…</code> ID, and negotiates SRTP audio.",
        "OpenAI holds the call and sends ZOVAK a <b>signed</b> <code>live.transport.incoming</code> webhook. ZOVAK verifies the signature with <code>OPENAI_WEBHOOK_SECRET</code> (a forged request is refused with 400), ignores repeats of the same event, and rate-limits the caller.",
        "ZOVAK accepts the session (the ID is passed back unchanged) with Nofa's phone instructions, then attaches the sideband (<code>channel=phone</code>). From here the call works exactly like a website call, except confirming sends the SMS form, and ending the call is a real hang-up (<code>live.sessions.hangup</code>).",
        "If ZOVAK doesn't accept (server down, wrong webhook URL), OpenAI can't connect the call and the caller hears nothing.",
    ) + boxes(
        ("Telnyx number (now) / Bangladeshi number (later)", "Carrier sends the call to sip:proj_…@sip.api.openai.com over TLS with SRTP audio"),
        ("OpenAI GPT-Live", "Sends a signed live.transport.incoming webhook to ZOVAK (through ngrok)"),
        ("ZOVAK /api/v1/phone/openai-webhook", "Verifies the signature, ignores duplicates, rate-limits the caller, accepts with Nofa's phone instructions"),
        ("VoiceSideband (channel=phone)", "Same booking rules, silence and guardrails; confirm sends the SMS form; ends with a real hang-up"),
    ) + py("zovak/api/routes/phone.py", "openai_webhook") +
        py("zovak/api/routes/phone.py", "caller_id") +
        py("zovak/voice/sideband.py", "VoiceSideband._phone_turn") + """
<h3>14.1 Diagnosing a call with no sound</h3>
<p>The uvicorn terminal shows ZOVAK's own timestamped lines. A healthy call looks like:</p>
<pre>Accepted phone call live_… from alice@sip.linphone.org (call audio: srtp)
GPT-Live sideband connected for phone call phone-…
Nofa is speaking (1.2 s after connect)       ← audio is being generated
Caller audio is arriving (4.8 s after connect) ← your voice reaches OpenAI
Caller said: 'I'd like to book a consultation tomorrow'
Nofa replies: '…'</pre>
""" + table(("What the log shows", "Meaning"),
            ("<code>call audio: rtp</code>", "Linphone didn't use encryption; GPT-Live needs SRTP"),
            ("No \"Nofa is speaking\"", "Nofa never produced a reply/greeting"),
            ("\"Nofa is speaking\" but you hear nothing", "Audio from OpenAI isn't reaching the phone (network/NAT)"),
            ("No \"Caller audio is arriving\"", "Your voice isn't reaching OpenAI; after 45 s ZOVAK ends the call as <code>no_audio</code>")) + """
<h3>14.2 Only the phone route and forms are public</h3>
<p>ngrok exposes port 8000, so ZOVAK refuses every request arriving through the tunnel except the
webhook and the form pages. The website talks to ZOVAK on 127.0.0.1 and is unaffected.</p>
""" + py("zovak/main.py", "_guard_public_host") + """
<h3>14.3 Working setup: Telnyx number → OpenAI (tested 2026-10-27)</h3>
<p>Calling OpenAI straight from a phone app (Linphone) connected but no audio ever flowed: phones
behind home routers or mobile networks can't receive the call audio. A SIP provider carries the
audio from public servers, which is what OpenAI expects. This setup worked end to end:</p>
""" + flow(
        "Telnyx: upgrade the account (a trial can only buy a number in your home country), then buy a US <b>local</b> number. Toll-free numbers can't be called from abroad.",
        "Voice → SIP Trunking → new <b>FQDN</b> connection \"OpenAI GPT-Live\": FQDN <code>sip.api.openai.com</code>, port <code>5061</code>, DNS record <code>A</code>, set as Primary FQDN.",
        "Inbound tab: SIP transport protocol <b>TLS</b>; codecs Opus, G711U, G711A. Configuration tab: Encrypted media <b>SRTP Mandatory</b> (it only saves once TLS is set).",
        "Phone Numbers → the number → Voice tab: SIP Connection = OpenAI GPT-Live; Advanced → <b>Translated Number</b> = your <code>proj_…</code> ID; Save Changes.",
        "OpenAI → Settings → Webhooks: <code>https://YOUR-NGROK/api/v1/phone/openai-webhook</code>, event <code>live.transport.incoming</code> (not <code>realtime.call.incoming</code>, which is the older Realtime API); signing secret into <code>OPENAI_WEBHOOK_SECRET</code>.",
        "Call the number. Say \"confirm\", then open the form from <code>/api/v1/dev/sms-outbox</code>.",
    ) + """
<h3>14.4 Linphone direct (didn't work: no audio)</h3>
<p>Kept for reference. The call was accepted and ZOVAK attached, but no audio packets ever flowed
(red signal in Linphone, no \"Nofa is speaking\", nothing billed), and the call closed after ~30 s.</p>
""" + flow(
        "OpenAI dashboard: check GPT-Live SIP is enabled for your project and copy the project ID (<code>proj_…</code>).",
        "Run <code>ngrok http 8000</code> and copy the https address.",
        "OpenAI dashboard → Webhooks: add <code>https://YOUR-NGROK/api/v1/phone/openai-webhook</code>, subscribe to <code>live.transport.incoming</code>, copy the signing secret.",
        "<code>.env</code>: <code>OPENAI_WEBHOOK_SECRET=whsec_…</code> and <code>PUBLIC_BASE_URL=https://YOUR-NGROK</code>; restart uvicorn and reconnect Google.",
        "Linphone: create or use a SIP account, set transport to TLS and media encryption to SRTP (mandatory).",
        "Dial <code>sip:proj_YOUR_ID@sip.api.openai.com:5061;transport=tls</code>, book, say \"confirm\".",
        "Open <code>http://127.0.0.1:8000/api/v1/dev/sms-outbox</code> on your laptop and tap the form link (it also works on your phone through ngrok).",
    ))

    add("""
<h2>15. The SMS booking form</h2>
""" + flow(
        "On the call, Nofa reads the details back. Saying \"confirm\" (a safe yes) does NOT book: it creates a one-time form and texts the link.",
        "Nofa says: \"I've sent you a text… If you want to change the slot or cancel the booking, you can still do it from the form. I'll end the call now.\" and hangs up.",
        "The form shows a 2:00 countdown; the slot is held for that time (other callers can't take it).",
        "The customer can edit the name, must add an email, may pick another free time, then taps Confirm appointment, or Cancel booking.",
        "Confirm books it (Google invite to the email). The link then only shows the result: it works once.",
    ) + py("zovak/services/booking_forms.py", "BookingForms.submit") +
        py("zovak/voice/sideband.py", "VoiceSideband._send_form") +
        py("zovak/services/mock_scheduler.py", "DemoScheduler._held_by_other") + """
<h3>15.1 Where a text actually goes: the SMS router</h3>
<p>Nofa and the form only ever call <code>send(to, text)</code>. The router decides the rest, so
adding or swapping a provider never touches the call or form code:</p>
""" + table(("Destination", "What happens", "Setting"),
            ("Test inbox", "Always: <code>/api/v1/dev/sms-outbox</code> on your laptop", "—"),
            ("ntfy push (testing)", "Free, instant notification on your phone; tapping it opens the form", "<code>TEST_SMS_NTFY_TOPIC</code>"),
            ("Telnyx (real SMS)", "Any phone number except the skipped prefixes. Not reliable without a registered sender: a Sweden test was accepted, then rejected by the carrier (40008)", "<code>TELNYX_API_KEY</code>, <code>TELNYX_FROM_NUMBER</code>, <code>TELNYX_MESSAGING_PROFILE_ID</code>"),
            ("Bangladesh (+880)", "Skipped by Telnyx (needs a registered sender: errors 40305/40306); test inbox + ntfy only, until a local gateway is added", "<code>TELNYX_SMS_SKIP_PREFIXES=+880</code>"),
            ("India", "Not set up: needs DLT registration (Indian entity, approved templates)", "—")) + """
<p>A provider that fails or rejects a text is logged (\"SMS to … via telnyx failed: …\"); the text
is still in the test inbox. Callers without a phone number (e.g. a SIP app address) only get the test
inbox. To add a provider later: write a class with <code>async send(to, body)</code> and register it,
e.g. <code>sms.add_provider("alpha-sms", AlphaSms(...), prefixes=("+880",))</code>.</p>
""" + py("zovak/providers/notifications/sms_router.py", "SmsRouter.send") +
        py("zovak/providers/notifications/ntfy_push.py", "NtfyPush.send") +
        py("zovak/providers/notifications/telnyx_sms.py", "TelnyxSms.send") +
        py("zovak/main.py", "lifespan", max_lines=30))

    add("""
<h2>16. WhatsApp chat and calls</h2>
<h3>16.1 Chatbot</h3>
<p>Each WhatsApp user is one conversation (<code>wa-&lt;number&gt;</code>), answered by the same
receptionist agent as the website chat. Tap buttons replace the website card.</p>
""" + flow(
        "Meta sends each message to <code>/api/v1/whatsapp/webhook</code>, signed with <code>X-Hub-Signature-256</code> (HMAC-SHA256 of the body with the app secret). ZOVAK checks it, replies 200 at once and processes in the background; repeated deliveries are ignored.",
        "Text goes to the agent (<code>channel=whatsapp</code>). If a booking is waiting for an email and the message contains one, ZOVAK attaches it directly.",
        "When a booking, move or cancellation is pending, ZOVAK sends tap buttons: Confirm booking / Change email, Confirm move / Keep current time, Cancel appointment / Keep it. Button IDs carry the pending item's ID and are checked against it.",
        "While the agent works, the message is marked read and WhatsApp shows \"typing…\" once (WhatsApp hides it after 25 s or when Nofa replies; no repeated refreshes, to keep API traffic human-paced).",
        "Guardrails: 30 messages an hour per user, more than 4 prepared changes a day stops changes, images and voice notes get \"please type\".",
    ) + py("zovak/services/whatsapp_bot.py", "WhatsAppBot._respond") +
        py("zovak/services/whatsapp_bot.py", "WhatsAppBot._button", max_lines=30) +
        py("zovak/api/routes/whatsapp.py", "receive") + """
<h3>16.2 Calls through the audio bridge</h3>
<p>WhatsApp user-initiated calls are free for the caller. Three routes didn't work: Meta SIP straight
to OpenAI (Meta puts the business number in the Request-URI, expects a digest challenge and the first
SRTP packet from our side); Telnyx WhatsApp calling (needs a 2,000-recipient messaging limit); and
handing Meta's WebRTC offer straight to GPT-Live — the logged SDPs show <b>both sides are ICE-lite</b>
(<code>a=ice-lite</code>), so neither starts the connection checks and no audio ever flows. A browser
works with GPT-Live because it's a full WebRTC client; the bridge plays that role on both sides.</p>
""" + boxes(
        ("WhatsApp caller → Meta", "Meta's calls webhook: connect event with its SDP offer (ICE-lite, setup:actpass)"),
        ("Bridge leg 1 (aiortc)", "Answers Meta's offer as a full ICE agent and starts the checks; sends silence until Nofa speaks"),
        ("Bridge leg 2 (aiortc)", "Makes its own offer to GPT-Live (audio + oai-events data channel), like the website's browser"),
        ("Relay", "Copies decoded audio frames both ways, resampled to mono 48 kHz, paced at real time, backlog capped"),
        ("Sideband", "Attaches to the GPT-Live session: same phone rules, form link sent on WhatsApp"),
    ) + flow(
        "Meta sends <code>connect</code> with the caller's SDP offer.",
        "ZOVAK makes the bridge's offer to GPT-Live (<code>live.create</code>, WebRTC) and applies GPT-Live's answer.",
        "ZOVAK answers Meta's offer with the bridge's other leg and sends that answer with <code>pre_accept</code> then <code>accept</code>.",
        "Audio flows caller ↔ Meta ↔ bridge ↔ GPT-Live. Every 5 s the log shows frames relayed each way. Hanging up closes both legs.",
        "The call uses the same conversation as the caller's WhatsApp chat (<code>wa-&lt;number&gt;</code>), so bookings and Nofa's memory are shared between chat and calls.",
    ) + py("zovak/services/whatsapp_calls.py", "WhatsAppCalls._connect") +
        py("zovak/voice/webrtc_bridge.py", "RelayTrack") +
        py("zovak/voice/webrtc_bridge.py", "AudioBridge.meta_answer") +
        py("zovak/voice/webrtc_bridge.py", "AudioBridge.openai_offer") + """
<h3>16.3 Setup (Meta, free test number)</h3>
""" + flow(
        "developers.facebook.com → My Apps → Create app → use case <b>Connect with customers through WhatsApp</b> (Business type) → link or create a business portfolio.",
        "WhatsApp → API Setup: note the <b>test number</b>, its <b>Phone number ID</b>, and generate a <b>temporary access token</b> (24 h). Add your own WhatsApp number under <b>To</b> and verify the code.",
        "App settings → Basic → <b>App secret</b>.",
        "<code>.env</code>: <code>WHATSAPP_ACCESS_TOKEN</code>, <code>WHATSAPP_PHONE_NUMBER_ID</code>, <code>WHATSAPP_APP_SECRET</code>, <code>WHATSAPP_VERIFY_TOKEN</code> (any long random string); restart uvicorn.",
        "WhatsApp → Configuration → Webhook: callback <code>https://YOUR-NGROK/api/v1/whatsapp/webhook</code>, verify token = the same string; subscribe to <b>messages</b> (and <b>calls</b> for the call experiment).",
        "<b>Subscribe your WhatsApp account to your app</b> — the dashboard's test flow subscribes it only to Meta's own test viewer (\"WA DevX Webhook Events\"), so messages appear in \"Check test webhooks\" but never reach ZOVAK: <code>curl -X POST https://graph.facebook.com/v25.0/WABA_ID/subscribed_apps -H \"Authorization: Bearer $WA_TOKEN\"</code>. Check with a GET on the same URL: your app must be listed.",
        "Calls: enable calling on the number (<code>POST /PHONE_NUMBER_ID/settings</code> with <code>calling.status=ENABLED</code>) and subscribe the <b>calls</b> webhook field.",
    ))

    add("""
<h2>17. Doctors and staff</h2>
<p>The demo clinic lives in <code>zovak/services/clinic.py</code> and is recreated on every start
(<code>DEMO_SEED=true</code> loads demo bookings for today and tomorrow).</p>
""" + table(("Doctor", "Specialty", "Services", "Hours", "Lunch"),
            ("Dr. Ayesha Rahman", "General Practice", "General consultation, Follow-up visit", "9 AM – 5 PM", "1–2 PM"),
            ("Dr. Imran Chowdhury", "General Practice", "General consultation, Follow-up visit", "10 AM – 6 PM", "2–3 PM"),
            ("Dr. Nusrat Sultana", "Pediatrics", "Child check-up, Child vaccination", "9 AM – 3 PM", "12–1 PM"),
            ("Dr. Farhan Karim", "Dermatology", "Skin consultation", "11 AM – 7 PM", "3–4 PM")) +
        table(("Staff (demo login)", "Role", "PIN"),
              ("Each doctor above", "doctor — sees own bookings", "1111 / 2222 / 3333 / 4444"),
              ("Rina Akter", "receptionist — sees everyone", "5555"),
              ("Tanvir Hasan", "clinic manager — sees everyone", "9999")) + """
<h3>17.1 Booking with a doctor</h3>
""" + flow(
        "Nofa first asks what kind of doctor or problem the patient has (no diagnosis).",
        "She calls <code>list_doctors</code> and matches the need to a specialty. If there's a match she calls <code>offer_doctors</code>: the website shows doctor cards (name, specialty, services, next free time; chat has a Choose button). On calls she says the service and only the matching doctors' names.",
        "She never lists every doctor: <code>offer_doctors</code> refuses doctors from more than one specialty, and asked \"which doctors do you have?\" she describes the kinds of care (general, children's, skin) and asks about the problem first.",
        "If nothing matches she says clearly that the clinic doesn't offer that service and lists what it does offer.",
        "Availability, holds and overlaps are per doctor, so two GPs can see patients at the same time. If no doctor is named, the first free qualified doctor is assigned.",
        "Google Calendar is one shared clinic calendar: ZOVAK subtracts its own bookings from Google's busy times (free/busy merges back-to-back events), so one doctor's booking doesn't block the others; outside events still block everyone.",
        "Bookings keep the caller's phone/WhatsApp number (<code>customer_phone</code>) so patients can be told about delays later.",
        "After a tap the card collapses to the picked time with a Change time link; the buttons only come back if the patient wants to change it.",
        "Once a doctor is chosen, the card shows their free times as buttons (next 3 days with openings). In chat a tap sends the time as the patient's message; on a website call it goes to ZOVAK, which handles it like speech and Nofa answers aloud. A tap never books; the patient still confirms.",
        "Picking a doctor (tapping Choose, typing or saying the name) is noticed by plain code before the AI runs: the cards are replaced by a \"Doctor selected\" card telling the patient to give a day and time, or ask for someone else. Naming two doctors or \"not Rahman\" isn't a pick, so the cards stay.",
    ) + py("zovak/services/clinic.py", "Doctor") +
        py("zovak/services/doctor_choice.py", "pick_offered_doctor") +
        py("zovak/services/booking_state.py", "free_days") +
        py("zovak/services/doctor_choice.py", "pick_tapped_time") +
        py("zovak/services/doctor_choice.py", "reopen_choice") +
        py("zovak/services/doctor_choice.py", "wants_new_email") +
        py("zovak/services/mock_scheduler.py", "DemoScheduler.reopen_times") +
        py("zovak/services/mock_scheduler.py", "DemoScheduler.start_time_change") +
        py("zovak/agents/runner.py", "stream_receptionist") +
        py("zovak/voice/sideband.py", "VoiceSideband.pick_slot") +
        ts("src/components/assistant/AssistantWidget.tsx", "function ChosenDoctor(", max_lines=45) +
        py("zovak/services/mock_scheduler.py", "DemoScheduler.offer_doctors") +
        py("zovak/services/mock_scheduler.py", "DemoScheduler.available_slots") +
        py("zovak/services/google_scheduler.py", "GoogleBackedScheduler._external_busy") +
        ts("src/components/assistant/AssistantWidget.tsx", "function DoctorCards(") +
        ts("src/components/assistant/AssistantWidget.tsx", "function ChosenDoctor(") + """
""")

    add("""
<h2>18. Delay recovery and the staff portal</h2>
<h3>18.1 Only the patients the delay reaches</h3>
<p><b>When the delay starts counting:</b> a time staff give ("30 minutes late for my 1:00");
otherwise now while the doctor is working, their start time if their day hasn't begun, or their next
day's start if today is over. A report at 10 PM therefore applies to tomorrow's patients.</p>
""" + table(("Report", "Counts from", "Result for Dr. Karim (11:00–7:00; patients 11:30, 12:00, 12:30, 1:00)"),
            ("10 PM: \"30 minutes late\"", "tomorrow 11:00", "Nobody: the delay ends at 11:30, in time for the 11:30 patient"),
            ("10 PM: \"30 minutes late for my 1:00\"", "tomorrow 1:00", "Only the 1:00 patient → 1:30"),
            ("11:15 AM (working): \"30 minutes late\"", "11:15", "11:30 → 11:45, 12:00 → 12:15, 12:30 → 12:45, 1:00 → 1:15")) + """
<p>Each of the doctor's remaining visits that day starts at <i>max(its own time, end of the previous
shifted visit)</i>, skipping lunch. As soon as a visit already starts after the doctor would be
free, the gap has absorbed the delay and nothing later changes.</p>
""" + table(("Dr. Rahman +60 min at 9:50", "Before", "After", "Why"),
            ("Ali", "10:00", "10:50", "affected"), ("Bina", "10:30", "11:20", "affected"),
            ("Chandni", "11:00", "11:50", "affected"), ("Dipu", "11:30", "12:20", "affected"),
            ("Eshita", "2:00 PM", "2:00 PM", "untouched: the gap before lunch absorbed the delay")) + """
<h3>18.2 Options per patient (best first)</h3>
""" + flow(
        "<b>No wait:</b> the same original time with another doctor who offers the service and is free (never offering one slot twice).",
        "<b>New time</b> with the same doctor: the shifted time. This is the default.",
        "<b>Other times</b> with the same doctor, only after they arrive: today, then tomorrow, avoiding the shifted visits.",
        "<b>Cancel.</b> If the shifted visit would run past closing, the default becomes the first next free time (\"needs a new time\").",
    ) + """
<h3>18.3 Preview, send, patient choice</h3>
""" + flow(
        "Staff (or the doctor) choose the delay and, optionally, <i>Late from</i> a specific day and time, then press <i>See who is affected</i>: a preview saying when it counts from; nothing changes and nobody is notified. If nobody is affected it says why and there's nothing to send.",
        "<i>Notify patients</i> applies every default time, latest first so shifts never collide, then sends each patient a message with a one-time link: SMS router (test inbox / ntfy / provider), WhatsApp if they booked there, and email if Resend is set up. Nofa's memory gets a note too.",
        "The patient opens <code>/update/&lt;token&gt;</code> and taps an option. Another doctor or another time moves the booking (Google Calendar is updated for live bookings); cancel cancels it. The link works once.",
        "Staff watch <i>Patient updates</i>: waiting, kept, rescheduled, cancelled, needs staff, or no reply after 15 minutes (they keep the new time).",
    ) + py("zovak/services/recovery.py", "RecoveryPlanner.delay_start") +
        py("zovak/services/recovery.py", "RecoveryPlanner.preview", max_lines=70) +
        py("zovak/services/recovery.py", "RecoveryPlanner._why_nobody") +
        py("zovak/services/recovery.py", "RecoveryPlanner.send") +
        py("zovak/services/recovery.py", "RecoveryPlanner.choose") +
        py("zovak/services/mock_scheduler.py", "DemoScheduler.staff_move") + """
<h3>18.4 Staff portal (/staff on the website)</h3>
<p><b>Where the doctor's schedule comes from:</b> every booking, whether made in website chat, a
website call, a phone call or the SMS form, is stored once in ZOVAK's booking store with its doctor.
The portal reads that same store, so a new booking shows in that doctor's login within 5 seconds.
Doctors have no calendars of their own; the one shared clinic Google Calendar only sends patients
their invites. The store is in memory for now (lost on restart) until saved bookings are planned.</p>
""" + table(("Tab", "What it does"),
            ("Sign in", "Name + demo PIN → session token (12 h; 5 wrong PINs lock that name for 5 minutes)"),
            ("Schedule", "Today, Tomorrow, or any day (‹ › or the date picker), per doctor; past visits greyed; delay status badges; refreshes every 5 s. Doctors see only themselves"),
            ("Running late", "Pick doctor (reception/manager), minutes and Late from (Now / a specific time) → preview → Notify patients or Discard"),
            ("Patient updates", "Every sent plan with each patient's status and choice, refreshed every few seconds"),
            ("Assistant", "Staff chat with Nofa: schedules, and \"I'm 40 minutes late\" or \"late for my 1:00\" → preview → explicit \"send it\". Replies are written out word by word and the chat keeps the newest message in view. Each sign-in has its own memory; the chat survives a reload and is cleared on sign-in/sign-out")) +
        py("zovak/api/routes/staff.py", "delay_preview") +
        py("zovak/agents/staff.py", "make_staff_agent") +
        py("zovak/agents/staff.py", "run_staff_agent") +
        py("zovak/services/staff_auth.py", "StaffSessions.login") +
        ts("src/app/staff/page.tsx", "function RunningLate(", max_lines=40) + """
""")

    add("""
<h2>19. The widget UI</h2>
<ul>
<li><b>Font:</b> Figtree for the widget (<code>src/app/layout.tsx</code>, Tailwind
<code>font-widget</code>).</li>
<li><b>Chat:</b> Nofa's bubbles are white with a small orb avatar; the customer's are teal; replies
stream in word by word; the list keeps scrolled to the newest message unless you scroll up.</li>
<li><b>Voice:</b> before content appears, a large orb; during a call with transcript or a card, a
slim header with a small orb, status and a live timer. The card never shrinks; the transcript scrolls
in the remaining space. Doctor suggestions on a call are a slim list (name, specialty, next free time)
that scrolls inside itself, so the transcript always stays visible.</li>
<li><b>Greeting:</b> "Hi, I'm Nofa, the front desk for Brightside Family Clinic. Tell me what you
need, and I'll find the right doctor and a time that suits you. I can also move or cancel a booking."
Buttons: I need to see a doctor · What services do you offer? · Change or cancel my booking.</li>
</ul>
""" + ts("src/components/assistant/AssistantWidget.tsx", "function useCallTimer(") +
        ts("src/components/assistant/AssistantWidget.tsx", "function BookingPanel(", max_lines=60))

    add("""
<h2>20. File map</h2>
""" + table(("File", "What it does"),
            ("<code>zovak/agents/receptionist.py</code>", "Agent instructions and tools: propose booking/move/cancel, current booking, report off-topic, today's date"),
            ("<code>zovak/services/mock_scheduler.py</code>", "Booking rules in memory: slots, proposals, confirm, move/cancel"),
            ("<code>zovak/services/google_scheduler.py</code>", "Same rules plus Google Calendar free/busy, create, move, delete"),
            ("<code>zovak/services/booking_state.py</code>", "Decides the card's stage, and the live status text given to the agent"),
            ("<code>zovak/agents/memory.py</code>", "Agent memory per conversation, plus notes about button actions"),
            ("<code>zovak/providers/calendar/google_invites.py</code>", "Google Calendar REST calls"),
            ("<code>zovak/voice/sideband.py</code>", "Live call brain: delegation, card actions, spoken confirmations, call ending"),
            ("<code>zovak/voice/confirmation.py</code>", "Spoken yes/no rules: natural but never a possibly-unfinished yes"),
            ("<code>zovak/voice/call_guard.py</code>", "Silence, off-topic and too-many-changes limits"),
            ("<code>zovak/api/routes/phone.py</code>", "Phone webhook: verify, accept SIP calls, attach the sideband"),
            ("<code>zovak/api/routes/forms.py</code>", "Public one-time SMS booking form pages"),
            ("<code>zovak/services/booking_forms.py</code>", "Form tokens, 2-minute holds, submit (confirm/cancel)"),
            ("<code>zovak/providers/notifications/sms_outbox.py</code>", "Development SMS test inbox"),
            ("<code>zovak/providers/notifications/sms_router.py</code>", "Sends each text to the test inbox, ntfy and the right provider by country"),
            ("<code>zovak/providers/notifications/ntfy_push.py</code>", "Free push-notification stand-in for SMS (testing)"),
            ("<code>zovak/providers/notifications/telnyx_sms.py</code>", "Telnyx SMS adapter"),
            ("<code>zovak/providers/notifications/whatsapp_meta.py</code>", "WhatsApp Cloud API client: text, buttons, read receipts, call actions"),
            ("<code>zovak/services/whatsapp_bot.py</code>", "WhatsApp chatbot with tap-button booking card"),
            ("<code>zovak/services/whatsapp_calls.py</code>", "WhatsApp calls: Meta webhook → audio bridge → GPT-Live + sideband"),
            ("<code>zovak/voice/webrtc_bridge.py</code>", "Audio bridge (aiortc) between two ICE-lite WebRTC servers"),
            ("<code>zovak/api/routes/whatsapp.py</code>", "Public WhatsApp webhook: verification and signed events"),
            ("<code>zovak/agents/runner.py</code>", "Runs one agent turn for text channels (website chat, WhatsApp)"),
            ("<code>zovak/services/clinic.py</code>", "Demo doctors, services, staff logins and demo bookings"),
            ("<code>zovak/services/recovery.py</code>", "Delay recovery: ripple planner, options, send, patient choice"),
            ("<code>zovak/services/staff_auth.py</code>", "Demo staff sign-in sessions"),
            ("<code>zovak/api/routes/staff.py</code>", "Staff portal API (local only)"),
            ("<code>zovak/agents/staff.py</code>", "Staff assistant agent"),
            ("<code>serena-health/src/app/staff/page.tsx</code>", "The staff portal page"),
            ("<code>zovak/voice/email_form.py</code>", "Typed email validation and gate"),
            ("<code>zovak/api/routes/dev.py</code>", "Chat routes: chat, confirm, booking-state, email, change"),
            ("<code>zovak/api/routes/voice_dev.py</code>", "Voice routes: session, booking-state, card actions (token required)"),
            ("<code>zovak/workers/reminders.py</code>", "1-hour reminder job"),
            ("<code>zovak/providers/notifications/resend_email.py</code>", "Resend email sender"),
            ("<code>serena-health/src/components/assistant/AssistantWidget.tsx</code>", "The widget: chat, voice, booking card, timer"),
            ("<code>serena-health/src/lib/agent/client.ts</code>", "Browser side: chat, voice WebRTC, card polling and actions"),
            ("<code>serena-health/src/app/api/agent/*</code>", "Next.js routes that forward to ZOVAK")))

    add("""
<h2>21. Limits and what's next</h2>
<ul>
<li><b>Memory only:</b> bookings, reminders and Google sign-in are lost when uvicorn restarts.
Move/cancel only works in the conversation where the booking was made (WhatsApp chat and WhatsApp
calls from one number share a conversation; phone and website don't link to it yet). A planned customer-tracking
system with saved bookings will fix this.</li>
<li><b>Development routes have no login</b> and must not be exposed publicly.</li>
<li><b>Voice timing is estimated:</b> there is no guaranteed "caller finished" signal, and the
goodbye wait is estimated from its length.</li>
<li><b>Phone:</b> Linphone calls are not yet tested against OpenAI end to end; a real number needs
a SIP provider with TLS + SRTP to OpenAI. Caller ID is untrusted, so calling back to change a booking
will need saved bookings plus a one-time SMS code.</li>
<li><b>SMS:</b> carriers reject texts from unregistered sender names (Sweden: 40008; Bangladesh:
40305/40306), so real SMS needs a registered sender or a local gateway per country; links may also
be filtered. Bangladesh needs a local gateway or a registered sender; India
needs DLT registration.</li>
</ul>
""")

    add(DECISIONS)

    rows = "".join(f'<div class="log"><span>{d}</span><div><b>{t}</b><p>{x}</p></div></div>'
                   for d, t, x in CHANGELOG)
    add(f"<h2>23. Change log</h2>{rows}")
    return "\n".join(s)


def _decision(problem: str, tried: list[tuple[str, str, str]], chosen: str, code: list[str]) -> str:
    rows = "".join(f"<tr><td>{a}</td><td class='{'ok' if r.startswith('✅') else 'bad'}'>{r}</td><td>{w}</td></tr>"
                   for a, r, w in tried)
    pointers = "".join(f"<li><code>{c}</code></li>" for c in code)
    return (f"<h3>{problem}</h3><table><thead><tr><th>Tried</th><th>Result</th><th>Why / evidence</th>"
            f"</tr></thead><tbody>{rows}</tbody></table><p><b>Chosen:</b> {chosen}</p>"
            f"<p class='small'><b>Code:</b></p><ul class='code-list'>{pointers}</ul>")


DECISIONS = "<h2>22. Why it's built this way (decision record)</h2>" + (
    "<p>Every connection in this system was tested in practice. This section keeps the reasons, "
    "so nobody has to rediscover them: what was tried, what happened, the evidence, and the code "
    "that implements the choice.</p>"
) + _decision(
    "22.1 Website ↔ ZOVAK",
    [("Browser calls ZOVAK directly", "❌ Not used", "ZOVAK has no CORS or login; exposing it would leak control of bookings and the OpenAI key"),
     ("Next.js server routes forward to ZOVAK", "✅ Chosen", "Browser only talks to its own site; ZOVAK stays on 127.0.0.1")],
    "The site's /api/agent/* routes call ZOVAK server-side with zovakFetch.",
    ["serena-health/src/lib/agent/zovak.ts → zovakFetch", "serena-health/src/app/api/agent/{chat,session,booking}/route.ts"],
) + _decision(
    "22.2 Confirming bookings",
    [("Let the AI book directly", "❌ Rejected", "An LLM can misread or be tricked; safety rule: the AI proposes, the customer confirms"),
     ("Strict spoken phrases only", "❌ Too rigid", "Customers had to say exact sentences; filler words broke it"),
     ("AI-classified \"yes\" (outside package)", "❌ Rejected", "Didn't install on our code, broke 12 tests, could misread \"yes, but…\""),
     ("Buttons + natural-but-safe spoken yes + 1.5 s silence check", "✅ Chosen", "An action word is required, hedge words block it, and a yes followed by more speech is ignored")],
    "Tap/click buttons everywhere; spoken yes only when complete and unconditional; phone confirmation via the SMS form.",
    ["zovak/voice/confirmation.py → is_approval, classify_confirmation", "zovak/voice/sideband.py → _settled, _commit_booking",
     "zovak/services/booking_state.py → booking_state", "serena-health AssistantWidget.tsx → BookingPanel"],
) + _decision(
    "22.3 Phone calls",
    [("Linphone app → OpenAI SIP directly", "❌ No audio", "Call set up and ZOVAK accepted, but no audio packets ever flowed: red signal in Linphone, no \"Nofa is speaking\", nothing billed, closed after ~30 s. A phone behind a router/mobile network can't receive OpenAI's audio"),
     ("Twilio trial", "❌ Not available", "No free SIP trunking on the trial"),
     ("Telnyx trial", "❌ Blocked", "Trial only sells a number in the account's home country (Bangladesh)"),
     ("Telnyx paid: US local number → FQDN SIP to OpenAI (TLS + SRTP)", "✅ Works", "Carrier servers carry the audio; ZOVAK code unchanged. A real Bangladeshi number later uses the same path")],
    "A SIP provider number routed to sip:proj_…@sip.api.openai.com; OpenAI's signed webhook; ZOVAK accepts and attaches the sideband.",
    ["zovak/api/routes/phone.py → openai_webhook, caller_id", "zovak/voice/sideband.py → _listen, _phone_turn, _send_form", "zovak/main.py → _guard_public_host"],
) + _decision(
    "22.4 Sending the booking form (SMS)",
    [("Telnyx SMS → Bangladesh", "❌ Rejected", "40305 (sender name not registered) / 40306 (international needs an alpha sender)"),
     ("Telnyx SMS → Sweden", "❌ Undelivered", "Accepted (queued) then 40008 \"carrier did not accept\""),
     ("ntfy push (testing)", "✅ Chosen for testing", "Free, instant; tapping opens the form"),
     ("Local BD gateway (e.g. Alpha SMS)", "⏳ Planned", "Cheap, registered/non-masking senders accepted by BD carriers")],
    "Every text goes through SmsRouter: always the test inbox, ntfy when set, a real provider chosen by country code. Adding a provider never touches call or form code.",
    ["zovak/providers/notifications/sms_router.py → SmsRouter", "zovak/providers/notifications/ntfy_push.py", "zovak/providers/notifications/telnyx_sms.py",
     "zovak/services/booking_forms.py", "zovak/api/routes/forms.py"],
) + _decision(
    "22.5 WhatsApp chat",
    [("Messages reached Meta but not ZOVAK", "❌ First attempt", "The test flow had subscribed the WhatsApp account only to Meta's own viewer app (\"WA DevX Webhook Events\"), so payloads showed in \"Check test webhooks\" only"),
     ("POST /{WABA_ID}/subscribed_apps with our app's token", "✅ Fixed", "Our app appears in subscribed_apps; messages arrive signed at /api/v1/whatsapp/webhook")],
    "Meta Cloud API test number (free, ≤5 recipients); same agent as website chat; tap buttons as the booking card; typing indicator.",
    ["zovak/services/whatsapp_bot.py → WhatsAppBot", "zovak/api/routes/whatsapp.py → verify, receive", "zovak/providers/notifications/whatsapp_meta.py → WhatsAppCloud"],
) + _decision(
    "22.6 WhatsApp calls",
    [("Meta SIP → OpenAI directly", "❌ Can't work", "Meta puts the business number in the Request-URI (OpenAI needs proj_…), expects a digest challenge and the first SRTP packet from our side"),
     ("Telnyx WhatsApp calling", "❌ Blocked", "Meta requires a 2,000-recipient messaging limit before calling (new accounts start lower)"),
     ("Hand Meta's WebRTC offer straight to GPT-Live", "❌ No audio", "Logged SDPs: Meta a=ice-lite + setup:actpass, GPT-Live a=ice-lite + setup:passive. Both only answer connection checks, neither starts them"),
     ("ZOVAK audio bridge (aiortc): full WebRTC client on both legs", "✅ Works", "Verified locally with two real peers, then a live WhatsApp call answered by Nofa")],
    "ZOVAK answers Meta's offer and calls GPT-Live like a browser, relaying audio (mono 48 kHz, real-time pacing, silence until Nofa speaks).",
    ["zovak/voice/webrtc_bridge.py → AudioBridge, RelayTrack", "zovak/services/whatsapp_calls.py → WhatsAppCalls._connect, sdp_summary"],
) + _decision(
    "22.7 The agent's memory",
    [("Agent memory only", "❌ Bug", "After a button confirmation the agent still believed the booking was pending and refused to move it"),
     ("Live booking status every turn + notes on button actions", "✅ Chosen", "The scheduler is the source of truth; the agent is told the current state")],
    "booking_status_for_agent in every agent turn; note_for_agent whenever something changes outside the agent.",
    ["zovak/services/booking_state.py → booking_status_for_agent", "zovak/agents/memory.py → note_for_agent"],
) + _decision(
    "22.8 Call guardrails",
    [("30 s silence timer", "❌ Too slow / gameable", "Coughs and \"uh\" reset it"),
     ("10 s warning + 10 s, real words only, noise-only turns end the call", "✅ Chosen", "Same rules on website, phone and WhatsApp calls; off-topic (3 strikes), too many changes, misuse report, per-caller call limit")],
    "CallGuard bookkeeping fed by the sideband; the AI can report off-topic and misuse but can't book.",
    ["zovak/voice/call_guard.py → CallGuard", "zovak/voice/sideband.py → _watch_silence, _caller_said, _noise_only_turn, _end",
     "zovak/agents/receptionist.py → report_off_topic, report_misuse, end_call"],
)


CSS = """
@page { size: A4; margin: 16mm 15mm; }
* { box-sizing: border-box; }
body { font: 10.5pt/1.55 -apple-system, "Helvetica Neue", Arial, sans-serif; color: #0f2b33; }
h1 { font-size: 26pt; line-height: 1.1; margin: 6px 0 10px; }
h2 { font-size: 15pt; margin: 26px 0 8px; padding-top: 6px; border-top: 2px solid #0e7490;
     break-after: avoid; }
h3 { font-size: 11.5pt; margin: 16px 0 6px; color: #0e7490; break-after: avoid; }
p, li { margin: 6px 0; }
code { font: 9pt "SF Mono", Menlo, monospace; background: #eaf3f3; padding: 1px 4px; border-radius: 4px; }
.cover { padding: 60px 0 30px; border-bottom: 1px solid #d5e3e5; }
.kicker { text-transform: uppercase; letter-spacing: .12em; font-size: 9pt; color: #0e7490; font-weight: 700; }
.sub { font-size: 13pt; color: #46606a; max-width: 34em; }
.meta { font-size: 9pt; color: #5b737c; }
.toc ol { columns: 2; font-size: 10pt; }
figure { margin: 10px 0 14px; break-inside: avoid-page; }
figcaption { font-size: 8.5pt; color: #46606a; background: #eaf3f3; padding: 5px 10px;
             border-radius: 6px 6px 0 0; border: 1px solid #d5e3e5; border-bottom: 0; }
pre { margin: 0; font: 8pt/1.45 "SF Mono", Menlo, monospace; background: #0b2730; color: #e3f1f3;
      padding: 10px 12px; border-radius: 0 0 6px 6px; white-space: pre-wrap; word-break: break-word; }
.cut { color: #7fb3bd; font-style: italic; }
.flow { list-style: none; padding: 0; margin: 10px 0; }
.flow li { display: flex; gap: 10px; margin: 0 0 6px; break-inside: avoid; }
.flow .n { flex: none; width: 22px; height: 22px; border-radius: 50%; background: #0e7490; color: #fff;
           font-weight: 700; font-size: 9pt; display: flex; align-items: center; justify-content: center; }
.boxes { margin: 12px 0; break-inside: avoid; }
.box { border: 1px solid #b9d3d7; border-radius: 8px; padding: 8px 12px; background: #f5f9f9; }
.box b { display: block; } .box span { font-size: 9.5pt; color: #46606a; }
.arrow { text-align: center; color: #0e7490; font-weight: 700; line-height: 1.2; }
table { width: 100%; border-collapse: collapse; margin: 10px 0; font-size: 9.5pt; break-inside: avoid; }
th, td { text-align: left; vertical-align: top; padding: 6px 8px; border-bottom: 1px solid #d5e3e5; }
th { background: #eaf3f3; }
.log { display: flex; gap: 14px; padding: 8px 0; border-bottom: 1px solid #d5e3e5; break-inside: avoid; }
.log > span { flex: none; width: 86px; font-weight: 700; color: #0e7490; }
.log p { margin: 2px 0 0; color: #46606a; }
.missing { color: #b45309; }
td.ok { color: #047857; font-weight: 700; white-space: nowrap; } td.bad { color: #b45309; font-weight: 700; white-space: nowrap; }
.code-list { margin-top: 0; } .code-list li { margin: 2px 0; }
"""


def main() -> None:
    page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f"<title>ZOVAK documentation</title><style>{CSS}</style></head>"
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
