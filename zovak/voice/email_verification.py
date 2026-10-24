"""Deterministic spoken email readback and two-stage consent gate.

This does NOT validate actual delivery or ownership of an email address. Only
an out-of-band verification link or code can provide that assurance.
"""
from dataclasses import dataclass
import re
from typing import Literal


class EmailSpeechError(ValueError):
    """Address contains characters we cannot spell unambiguously by voice."""


_DIGITS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
}
_PUNCTUATION = {"@": "at sign", ".": "dot", "_": "underscore",
                "-": "hyphen", "+": "plus sign"}


def spell_email_for_voice(email: str) -> str:
    """Spell characters as spoken words; never change the actual destination.

    This intentionally avoids reading strings like 'amartyaghosh40' as one word.
    """
    if not isinstance(email, str) or len(email) > 254 or not email:
        raise EmailSpeechError("Invalid email length")
    if email.count('@') != 1 or email.startswith('@') or email.endswith('@'):
        raise EmailSpeechError("Expected one valid at sign")
    local, domain = email.split('@')
    if not local or not domain or '.' not in domain or any(c.isspace() for c in email):
        raise EmailSpeechError("Email format cannot be safely read by voice")

    spoken = []
    for char in email:
        if char in _PUNCTUATION:
            spoken.append(_PUNCTUATION[char])
        elif char in _DIGITS:
            spoken.append(f"number {_DIGITS[char]}")
        elif "a" <= char <= "z":
            spoken.append(f"lowercase {char.upper()}")
        elif "A" <= char <= "Z":
            spoken.append(f"uppercase {char}")
        else:
            # Don't fake pronunciations of unusual punctuation or Unicode.
            raise EmailSpeechError("Email contains a character requiring visual verification")
    return ", ".join(spoken)


EmailDecision = Literal["correct", "change", "cancel", "unclear"]


def classify_email_reply(text: str) -> EmailDecision:
    """Consent to the email ONLY. Never consent to a booking here."""
    normalized = re.sub(r"[^a-z0-9' ]", " ", text.lower().replace('’', "'"))
    normalized = " ".join(normalized.split())
    if normalized in {
        "yes", "yes that's correct", "yes that is correct",
        "that's correct", "that is correct", "correct", "that's right",
        "yes the email is correct", "yes email is correct", "the email is correct",
        "email is correct", "yes that's my email", "yes that is my email",
        "yes you got the email right", "yes the email address is correct",
    }:
        return "correct"
    if normalized in {"cancel", "cancel appointment", "cancel the booking", "stop booking"}:
        return "cancel"
    if normalized in {
        "no", "nope", "wrong", "that's wrong", "that is wrong",
        "no that's wrong", "no that is wrong", "no the email is wrong",
        "change my email", "change the email", "change email", "wrong email",
        "that's not my email", "that is not my email",
    }:
        return "change"
    # A sentence containing booking consent PLUS a change is never approval.
    if any(word in normalized for word in (
        "wrong email", "change the email", "change my email", "email is wrong",
        "email is incorrect", "correct the email", "change email",
    )):
        return "change"
    return "unclear"


@dataclass
class EmailVerificationGate:
    proposal_id: str | None = None
    email: str | None = None
    verified: bool = False
    correction_requested: bool = False

    def arm(self, proposal_id: str, email: str) -> None:
        self.proposal_id = proposal_id
        self.email = email
        self.verified = False
        self.correction_requested = False

    def clear(self) -> None:
        self.proposal_id = None
        self.email = None
        self.verified = False
        self.correction_requested = False


def email_question(email: str) -> str:
    """Backend-created speech: independent of the language model's spelling."""
    spoken = spell_email_for_voice(email)
    return (
        "Before booking, I need to verify your email address carefully. "
        "I'll spell each character. "
        f"{spoken}. "
        "Is that exact email address correct? "
        "Please say, 'Yes, the email is correct,' or 'No, change the email.' "
        "I have not booked anything or sent an invitation."
    )
