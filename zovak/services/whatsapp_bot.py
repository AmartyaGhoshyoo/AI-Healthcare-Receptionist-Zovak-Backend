"""Nofa on WhatsApp chat: the same receptionist agent, with tap buttons as the booking card.

Each WhatsApp user is one conversation (`wa-<number>`), so while the server runs
they can book, then later move or cancel in the same chat. Nothing is booked,
moved or cancelled until the customer taps the matching button; button IDs are
checked against the single pending item, like the website card.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict, deque

from zovak.agents.memory import note_for_agent
from zovak.agents.runner import run_receptionist
from zovak.services.booking_state import booking_state
from zovak.services.mock_scheduler import SchedulingError
from zovak.voice.email_form import validate_typed_email

logger = logging.getLogger(__name__)

TENANT = "demo-business"
ACTION_LIMIT = 4  # bookings/moves/cancellations prepared per chat per day
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_ONLY_TEXT = "I can only read text messages. Please type what you'd like to book, move or cancel."
_TOO_MANY = "You've sent a lot of messages. Please wait a while, or call us to book."
_STALE = "That option is no longer available. Tell me what you'd like to do."
_ANYTHING_ELSE = " Is there anything else I can help with?"
_CARD_STAGES = ("email_required", "ready_to_confirm", "move_pending", "cancel_pending")


class WhatsAppBot:
    def __init__(self, scheduler, cloud, *, run_agent=run_receptionist,
                 max_messages_per_hour: int = 30):
        self.scheduler = scheduler
        self.cloud = cloud
        self.run_agent = run_agent
        self.max_per_hour = max_messages_per_hour
        self._seen: OrderedDict[str, None] = OrderedDict()  # Meta retries deliveries
        self._locks: dict[str, asyncio.Lock] = {}
        self._recent: dict[str, deque[float]] = {}
        self._warned: set[str] = set()
        self._last_card: dict[str, tuple] = {}
        self._actions: dict[str, list[float]] = {}

    # ── Incoming webhook payloads ─────────────────────────────────────────────

    async def handle_payload(self, payload: dict) -> None:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("field") != "messages":
                    continue
                for message in change.get("value", {}).get("messages", []):
                    await self._handle_message(message)

    async def _handle_message(self, message: dict) -> None:
        message_id, sender = message.get("id", ""), message.get("from", "")
        if not message_id or not sender or message_id in self._seen:
            return
        self._seen[message_id] = None
        while len(self._seen) > 5000:
            self._seen.popitem(last=False)
        async with self._locks.setdefault(sender, asyncio.Lock()):
            try:
                # Blue ticks + "typing…" once, like a person (WhatsApp hides it after 25 s
                # or when we reply). No repeated refreshes: keep API traffic human-paced.
                await self.cloud.mark_read(message_id, typing=True)
            except Exception:
                logger.debug("Could not show WhatsApp typing indicator", exc_info=True)
            try:
                await self._respond(sender, message)
            except Exception:
                logger.exception("WhatsApp reply to %s failed", sender)

    async def _respond(self, sender: str, message: dict) -> None:
        if self._rate_limited(sender):
            if sender not in self._warned:
                self._warned.add(sender)
                await self.cloud.send_text(sender, _TOO_MANY)
            return
        conversation = f"wa-{sender}"
        kind = message.get("type")
        if kind == "interactive" and message.get("interactive", {}).get("type") == "button_reply":
            await self._button(sender, conversation, message["interactive"]["button_reply"]["id"])
            return
        if kind != "text":
            await self.cloud.send_text(sender, _ONLY_TEXT)
            return
        text = message.get("text", {}).get("body", "").strip()
        logger.info("WhatsApp %s said: %r", sender, text[:160])
        if await self._take_email(sender, conversation, text):
            return
        reply = await self.run_agent(self.scheduler, TENANT, conversation, text, channel="whatsapp",
                                     customer_phone=f"+{sender}")
        await self.cloud.send_text(sender, reply)
        await self._show_card(sender, conversation)

    def _rate_limited(self, sender: str) -> bool:
        now = time.monotonic()
        recent = self._recent.setdefault(sender, deque())
        while recent and now - recent[0] > 3600:
            recent.popleft()
        recent.append(now)
        if len(recent) <= self.max_per_hour:
            self._warned.discard(sender)
            return False
        return True

    # ── Email typed in the chat ───────────────────────────────────────────────

    async def _take_email(self, sender: str, conversation: str, text: str) -> bool:
        """If a booking is waiting for an email and the message contains one, attach it."""
        pending = await self.scheduler.latest_pending(TENANT, conversation)
        found = _EMAIL.search(text)
        if pending is None or pending.customer_email or not found:
            return False
        try:
            email = validate_typed_email(found.group(0).strip(".,;:"))
            await self.scheduler.set_proposal_email(TENANT, conversation, pending.id, email)
        except (ValueError, SchedulingError) as exc:
            await self.cloud.send_text(sender, f"That email doesn't look right ({exc}). Please type it again.")
            return True
        await note_for_agent(TENANT, conversation, "The customer typed their email in WhatsApp.")
        await self._show_card(sender, conversation)
        return True

    # ── The booking card as tap buttons ───────────────────────────────────────

    def _over_limit(self, conversation: str) -> bool:
        now = time.monotonic()
        actions = [t for t in self._actions.get(conversation, []) if now - t < 86400]
        actions.append(now)
        self._actions[conversation] = actions
        return len(actions) > ACTION_LIMIT

    async def _show_card(self, sender: str, conversation: str) -> None:
        pending = await self.scheduler.latest_pending(TENANT, conversation)
        state = await booking_state(
            self.scheduler, TENANT, conversation, proposal=pending,
            email_ready=bool(pending and pending.customer_email), email_request_id=None,
        )
        stage = state["stage"]
        item = state["proposal"] or state["change"] or {}
        key = (stage, item.get("id"))
        last = self._last_card.get(conversation)
        if key == last or stage not in _CARD_STAGES:
            return
        new_item = key[1] != (last[1] if last else None)
        if new_item and self._over_limit(conversation):
            # Booking, moving and cancelling over and over: stop before mistakes happen.
            if pending is not None:
                await self.scheduler.discard(TENANT, conversation, pending.id)
            if state["change"]:
                await self.scheduler.discard_change(TENANT, conversation, state["change"]["id"])
            self._last_card.pop(conversation, None)
            await self.cloud.send_text(sender, "To avoid mistakes, I can't make more changes in "
                                               "this chat today. Please call us if you need help.")
            return
        self._last_card[conversation] = key
        tz = self.scheduler.timezone
        if stage == "email_required":
            await self.cloud.send_text(sender, "Please type the email address for your calendar invitation.")
        elif stage == "ready_to_confirm":
            p = state["proposal"]
            await self.cloud.send_buttons(sender, (
                f"Please check your appointment:\n*{p['service_name']}*\n"
                f"{_when(p['start'], tz)}\nName: {p['customer_name']}\nEmail: {p['customer_email']}\n"
                "Nothing is booked until you tap Confirm."
            ), [(f"book:{p['id']}", "Confirm booking"), (f"email:{p['id']}", "Change email")])
        elif stage == "move_pending":
            b, c = state["booking"], state["change"]
            await self.cloud.send_buttons(sender, (
                f"Move your *{b['service_name']}*\nfrom {_when(b['start'], tz)}\n"
                f"to *{_when(c['new_start'], tz)}*?"
            ), [(f"move:{c['id']}", "Confirm move"), (f"keep:{c['id']}", "Keep current time")])
        elif stage == "cancel_pending":
            b, c = state["booking"], state["change"]
            await self.cloud.send_buttons(sender, (
                f"Cancel your *{b['service_name']}* on {_when(b['start'], tz)}?"
            ), [(f"cancel:{c['id']}", "Cancel appointment"), (f"keep:{c['id']}", "Keep it")])

    async def _button(self, sender: str, conversation: str, button_id: str) -> None:
        action, _, item_id = button_id.partition(":")
        scheduler = self.scheduler
        pending = await scheduler.latest_pending(TENANT, conversation)
        change = await scheduler.latest_change(TENANT, conversation)
        try:
            if action in ("book", "email"):
                if pending is None or pending.id != item_id:
                    await self.cloud.send_text(sender, _STALE)
                    return
                if action == "email":
                    await scheduler.clear_proposal_email(TENANT, conversation, pending.id)
                    await self._show_card(sender, conversation)
                    return
                result = await scheduler.confirm(TENANT, conversation, pending.id)
                self._last_card.pop(conversation, None)
                await note_for_agent(TENANT, conversation, "The customer tapped Confirm booking. "
                                                           "The appointment is now CONFIRMED.")
                invite = (" A calendar invitation is on its way to your email."
                          if result.provider == "google-calendar" else "")
                await self.cloud.send_text(sender, f"✅ You're booked: {_when(pending.start.isoformat(), scheduler.timezone)}."
                                                   + invite + _ANYTHING_ELSE)
                return
            if change is None or change.id != item_id:
                await self.cloud.send_text(sender, _STALE)
                return
            self._last_card.pop(conversation, None)
            if action == "keep":
                await scheduler.discard_change(TENANT, conversation, change.id)
                await note_for_agent(TENANT, conversation, "The customer chose to keep the appointment unchanged.")
                await self.cloud.send_text(sender, "No problem, your appointment stays as it was." + _ANYTHING_ELSE)
                return
            if action not in ("move", "cancel") or action != change.kind:
                await self.cloud.send_text(sender, _STALE)
                return
            result = await scheduler.apply_change(TENANT, conversation, change.id)
            if result.status == "moved":
                await note_for_agent(TENANT, conversation, "The customer confirmed the move; the booking is now at the new time.")
                await self.cloud.send_text(sender, f"✅ Moved to {_when(change.new_start.isoformat(), scheduler.timezone)}."
                                                   + _ANYTHING_ELSE)
            else:
                await note_for_agent(TENANT, conversation, "The customer confirmed the cancellation; the booking is cancelled.")
                await self.cloud.send_text(sender, "Your appointment is cancelled." + _ANYTHING_ELSE)
        except SchedulingError as exc:
            await self.cloud.send_text(sender, f"Sorry, I couldn't do that: {exc}.")


def _when(iso: str, tz) -> str:
    from datetime import datetime
    return datetime.fromisoformat(iso).astimezone(tz).strftime("%a %b %d, %I:%M %p")
