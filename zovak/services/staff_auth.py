"""Demo staff login: name + PIN → a session token. DEMO ONLY, not real authentication."""
from __future__ import annotations

import time
from secrets import compare_digest, token_hex, token_urlsafe

from zovak.services.clinic import STAFF, StaffMember

SESSION_HOURS = 12
MAX_ATTEMPTS = 5  # wrong PINs per staff member per 5 minutes


class StaffSessions:
    def __init__(self):
        self._sessions: dict[str, tuple[str, float]] = {}
        # A fresh assistant memory per sign-in, so it never recalls plans from an old session.
        # Its own random id: the sign-in token itself is never written into the memory file.
        self._chats: dict[str, str] = {}
        self._failures: dict[str, list[float]] = {}

    def login(self, staff_id: str, pin: str) -> tuple[str, StaffMember] | None:
        member = STAFF.get(staff_id)
        now = time.monotonic()
        recent = [t for t in self._failures.get(staff_id, []) if now - t < 300]
        if member is None or len(recent) >= MAX_ATTEMPTS:
            return None
        if not compare_digest(member.pin, pin):
            self._failures[staff_id] = recent + [now]
            return None
        token = token_urlsafe(32)
        self._sessions[token] = (staff_id, now + SESSION_HOURS * 3600)
        self._chats[token] = token_hex(8)
        return token, member

    def get(self, token: str | None) -> StaffMember | None:
        session = self._sessions.get(token or "")
        if session is None or session[1] < time.monotonic():
            return None
        return STAFF[session[0]]

    def chat_id(self, token: str | None) -> str:
        """The assistant conversation for this sign-in."""
        return self._chats[token or ""]

    def logout(self, token: str) -> None:
        self._sessions.pop(token, None)
        self._chats.pop(token, None)
