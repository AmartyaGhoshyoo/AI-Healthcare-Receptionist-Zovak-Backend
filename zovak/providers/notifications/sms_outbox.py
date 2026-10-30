"""Development SMS sender: keeps texts in memory instead of sending them.

Free testing (e.g. Linphone calls) has no real SMS. Open /api/v1/dev/sms-outbox
to read what would have been sent and tap the links. A real provider (Twilio,
a Bangladeshi SMS gateway...) later implements the same send() method.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class SmsMessage:
    to: str
    body: str
    sent_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class DevSmsOutbox:
    def __init__(self, keep: int = 50):
        self.messages: list[SmsMessage] = []
        self.keep = keep

    async def send(self, to: str, body: str) -> None:
        self.messages.append(SmsMessage(to=to, body=body))
        del self.messages[:-self.keep]
        logger.info("DEV SMS to %s: %s", to, body)
