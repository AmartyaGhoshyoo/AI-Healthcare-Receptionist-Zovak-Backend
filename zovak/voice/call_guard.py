"""Ends a web voice call that has gone quiet or keeps straying from appointments.

Pure bookkeeping, so it can be tested without a live call; VoiceSideband feeds
it events and acts on what tick() and strike() return.
"""
from __future__ import annotations

from typing import Literal

SILENCE_LIMIT = 20.0  # 10 s of silence → warning, 10 s more → the call ends
EMAIL_LIMIT = 120.0  # longer while the email box is open: typing takes time
CHECK_IN_BEFORE = 10.0  # the warning comes this long before ending
OFF_TOPIC_LIMIT = 3  # the call ends on this off-topic request
ACTION_LIMIT = 4  # bookings/moves/cancellations prepared in one call before it ends
NOISE_LIMIT = 3  # turns with only coughs/"uh"/"hmm" before the call ends

Action = Literal["check_in", "end"] | None


def speech_seconds(text: str) -> float:
    """Rough time for Nofa to say text aloud (about 14 characters a second)."""
    return len(text) / 14 + 1


class CallGuard:
    def __init__(self, *, silence_limit: float = SILENCE_LIMIT, email_limit: float = EMAIL_LIMIT,
                 check_in_before: float = CHECK_IN_BEFORE, off_topic_limit: int = OFF_TOPIC_LIMIT,
                 action_limit: int = ACTION_LIMIT):
        self.silence_limit = silence_limit
        self.email_limit = email_limit
        self.check_in_before = check_in_before
        self.off_topic_limit = off_topic_limit
        self.action_limit = action_limit
        self.actions = 0
        self.noise_turns = 0
        self.idle = 0.0
        self.checked_in = False
        self.speaking_until = 0.0
        self.off_topic = 0
        self.ending = False

    def caller_active(self) -> None:
        """The caller spoke or tapped a button: restart the silence count."""
        self.idle = 0.0
        self.checked_in = False

    def agent_speaking(self, now: float, seconds: float) -> None:
        """Nofa is talking; time spent listening to her isn't caller silence."""
        self.speaking_until = max(self.speaking_until, now + seconds)

    def tick(self, seconds: float, now: float, *, busy: bool, email_open: bool) -> Action:
        """Advance the silence count. busy = the backend is still working on a reply."""
        if self.ending or busy or now < self.speaking_until:
            return None
        self.idle += seconds
        limit = self.email_limit if email_open else self.silence_limit
        if self.idle >= limit:
            return "end"
        if not self.checked_in and self.idle >= limit - self.check_in_before:
            self.checked_in = True
            return "check_in"
        return None

    def strike(self) -> int:
        """Record one off-topic request; returns how many so far."""
        self.off_topic += 1
        return self.off_topic

    @property
    def out_of_strikes(self) -> bool:
        return self.off_topic >= self.off_topic_limit

    def count_action(self) -> bool:
        """A new booking, move or cancellation was prepared. True when over the limit:
        someone booking, moving and cancelling over and over is wasting the line."""
        self.actions += 1
        return self.actions > self.action_limit

    def noise_turn(self) -> bool:
        """A turn with only noise (cough, "uh", "hmm"). True when the call should end:
        right after the silence warning (pretending to answer), or on the 3rd one."""
        self.noise_turns += 1
        return self.checked_in or self.noise_turns >= NOISE_LIMIT
