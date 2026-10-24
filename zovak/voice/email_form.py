"""Local-dev visual email entry gate. A typed email is NOT booking consent."""
from __future__ import annotations

from dataclasses import dataclass
from secrets import token_urlsafe
import re

from zovak.services.mock_scheduler import SchedulingError


_LOCAL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+\Z")
_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def validate_typed_email(value: str) -> str:
    """Validate a common ASCII email, preserving local-part case and punctuation.

    This does not verify delivery or mailbox ownership. Internationalized,
    quoted or unusually formatted addresses need a separate workflow.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("Enter an email address without leading/trailing spaces")
    if not value.isascii() or len(value) > 254 or value.count("@") != 1:
        raise ValueError("Please enter a standard email address")
    local, domain = value.split("@")
    if (not 1 <= len(local) <= 64 or not _LOCAL.fullmatch(local)
            or local.startswith(".") or local.endswith(".") or ".." in local):
        raise ValueError("Invalid email username")
    labels = domain.split(".")
    if len(domain) > 253 or len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels):
        raise ValueError("Invalid email domain")
    if len(labels[-1]) < 2:
        raise ValueError("Invalid email domain ending")
    return local + "@" + domain.lower()


@dataclass
class EmailFormGate:
    proposal_id: str | None = None
    request_id: str | None = None
    verified_email: str | None = None

    def arm(self, proposal_id: str) -> None:
        if self.proposal_id != proposal_id:
            self.proposal_id = proposal_id
            self.request_id = token_urlsafe(24)
            self.verified_email = None

    @property
    def verified(self) -> bool:
        return self.proposal_id is not None and self.verified_email is not None

    def clear(self) -> None:
        self.proposal_id = None
        self.request_id = None
        self.verified_email = None
