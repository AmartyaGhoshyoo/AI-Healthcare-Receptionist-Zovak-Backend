"""Sends each text to the right place. Nofa and the SMS form only call send().

  test inbox  — always (development)
  ntfy        — if TEST_SMS_NTFY_TOPIC is set (free push to your phone, testing)
  provider    — the first registered provider whose country prefixes match the
                number (e.g. "+880" → a Bangladeshi gateway, "" → any number)

Adding a provider later (Alpha SMS, BulkSMS BD, Twilio...) = one adapter class
with an async send(to, body) method, registered here with its prefixes.
A failing or missing provider is logged; the text is still in the test inbox.
"""
from __future__ import annotations

import logging
import re
from typing import Protocol

from zovak.providers.notifications.sms_outbox import DevSmsOutbox

logger = logging.getLogger(__name__)
_E164 = re.compile(r"\+\d{6,15}")


class SmsSender(Protocol):
    async def send(self, to: str, body: str) -> object: ...


class SmsRouter:
    def __init__(self, outbox: DevSmsOutbox | None = None):
        self.outbox = outbox or DevSmsOutbox()
        self.test_push: SmsSender | None = None
        self._providers: list[tuple[tuple[str, ...], tuple[str, ...], str, SmsSender]] = []

    @property
    def messages(self):
        return self.outbox.messages

    def add_provider(self, name: str, sender: SmsSender, *, prefixes: tuple[str, ...] = ("",),
                     skip: tuple[str, ...] = ()) -> None:
        """Use sender for numbers starting with any of prefixes ("" = all), except skip."""
        self._providers.append((prefixes, skip, name, sender))

    def provider_for(self, to: str) -> tuple[str, SmsSender] | None:
        for prefixes, skip, name, sender in self._providers:
            if to.startswith(prefixes) and not (skip and to.startswith(skip)):
                return name, sender
        return None

    async def send(self, to: str, body: str) -> None:
        await self.outbox.send(to, body)
        if self.test_push is not None:
            try:
                await self.test_push.send(to, body)
            except Exception:
                logger.warning("Test push (ntfy) failed", exc_info=True)
        if not _E164.fullmatch(to):
            logger.info("No real SMS: %r isn't a phone number (test inbox only)", to)
            return
        match = self.provider_for(to)
        if match is None:
            logger.info("No SMS provider for %s yet (test inbox only)", to)
            return
        name, sender = match
        try:
            message_id = await sender.send(to, body)
            logger.info("SMS to %s accepted by %s (%s)", to, name, message_id)
        except Exception as exc:
            logger.warning("SMS to %s via %s failed: %s", to, name, exc)
