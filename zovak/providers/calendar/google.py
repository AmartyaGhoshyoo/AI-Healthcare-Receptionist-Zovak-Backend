"""Async Google Calendar REST adapter: credentials supplied by trusted OAuth layer.

NOT yet wired to booking DB. Calendar API reads/writes are not atomic together.
"""
from datetime import datetime
from urllib.parse import quote

import httpx


class GoogleCalendar:
    def __init__(self, access_token: str, calendar_id: str = "primary", *, client: httpx.AsyncClient | None = None):
        self._token = access_token
        self.calendar_id = calendar_id
        self._client = client  # caller owns optional injected client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    @property
    def _url(self) -> str:
        return "https://www.googleapis.com/calendar/v3/calendars/" + quote(self.calendar_id, safe="")

    async def is_available(self, start: datetime, end: datetime) -> bool:
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError("Timezone-aware start < end required")
        async with httpx.AsyncClient(timeout=15) if self._client is None else _shared(self._client) as client:
            response = await client.post(
                "https://www.googleapis.com/calendar/v3/freeBusy",
                headers=self._headers(),
                json={"timeMin": start.isoformat(), "timeMax": end.isoformat(),
                      "items": [{"id": self.calendar_id}]},
            )
            response.raise_for_status()
            result = response.json().get("calendars", {}).get(self.calendar_id, {})
            if result.get("errors"):
                raise RuntimeError("Google Calendar returned a free/busy error")
            if self.calendar_id not in response.json().get("calendars", {}):
                raise RuntimeError("Calendar missing from free/busy response")
            return not result.get("busy")

    async def create_event(self, title: str, start: datetime, end: datetime, event_id: str) -> str:
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError("Timezone-aware start < end required")
        # Caller must provide a Google-compatible event ID; Google has identifier restrictions.
        body = {"id": event_id, "summary": title,
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": end.isoformat()}}
        async with httpx.AsyncClient(timeout=15) if self._client is None else _shared(self._client) as client:
            response = await client.post(self._url + "/events", json=body, headers=self._headers())
            response.raise_for_status()
            result = response.json()
            if result.get("status") == "cancelled" or not result.get("id"):
                raise RuntimeError("Google Calendar did not confirm active event creation")
            return str(result["id"])

    async def delete_event(self, event_id: str) -> None:
        async with httpx.AsyncClient(timeout=15) if self._client is None else _shared(self._client) as client:
            response = await client.delete(self._url + "/events/" + quote(event_id, safe=""),
                                           headers=self._headers())
            response.raise_for_status()


class _shared:
    """Async context manager that does not close an injected client."""
    def __init__(self, client: httpx.AsyncClient): self.client = client
    async def __aenter__(self): return self.client
    async def __aexit__(self, *args): return False
