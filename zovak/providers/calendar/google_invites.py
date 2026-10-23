"""Google Calendar free/busy and invitation APIs (dev single calendar)."""
from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

import httpx

from zovak.providers.calendar.google import GoogleCalendar, _shared


class GoogleInvites(GoogleCalendar):
    async def busy_intervals(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
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
            result = response.json().get("calendars", {}).get(self.calendar_id)
            if result is None or result.get("errors"):
                raise RuntimeError("Google Calendar free/busy response missing or invalid")
            intervals = []
            for busy in result.get("busy", []):
                a, b = datetime.fromisoformat(busy["start"].replace("Z", "+00:00")), datetime.fromisoformat(busy["end"].replace("Z", "+00:00"))
                if a.tzinfo is None or b.tzinfo is None or a >= b:
                    raise RuntimeError("Invalid Google free/busy interval")
                intervals.append((a, b))
            return intervals

    async def invited_event_exists(self, *, event_id: str, start: datetime,
                                  end: datetime, customer_email: str) -> bool:
        """Detect a successful prior insert after an ambiguous timeout."""
        async with httpx.AsyncClient(timeout=15) if self._client is None else _shared(self._client) as client:
            response = await client.get(
                self._url + "/events/" + quote(event_id, safe=""),
                headers=self._headers(),
            )
            if response.status_code == 404:
                return False
            response.raise_for_status()
            result = response.json()
            attendees = {str(a.get("email", "")).casefold() for a in result.get("attendees", [])}
            if (result.get("id") != event_id
                or result.get("status") == "cancelled"
                or customer_email.casefold() not in attendees
                or not result.get("start", {}).get("dateTime")
                or not result.get("end", {}).get("dateTime")
                or datetime.fromisoformat(result["start"]["dateTime"].replace("Z", "+00:00")) != start
                or datetime.fromisoformat(result["end"]["dateTime"].replace("Z", "+00:00")) != end):
                raise RuntimeError("Event ID exists but is not the proposed appointment")
            return True

    async def create_invited_event(self, *, event_id: str, title: str,
                                   start: datetime, end: datetime,
                                   customer_email: str, description: str = "") -> str:
        """Create organizer event; request Google to email invitation to attendee.

        The invitation appearing in customer's calendar depends on their settings/RSVP.
        Caller owns locking, availability checks, token refresh and retry policy.
        """
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError("Timezone-aware start < end required")
        body = {
            "id": event_id,
            "summary": title,
            "description": description,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
            "attendees": [{"email": customer_email}],
            "guestsCanModify": False,
            "guestsCanInviteOthers": False,
        }
        async with httpx.AsyncClient(timeout=15) if self._client is None else _shared(self._client) as client:
            response = await client.post(
                self._url + "/events", params={"sendUpdates": "all"},
                headers=self._headers(), json=body,
            )
            if response.status_code == 409:
                if await self.invited_event_exists(
                    event_id=event_id, start=start, end=end,
                    customer_email=customer_email,
                ):
                    return event_id
                raise RuntimeError("Conflicting Google event ID")
            response.raise_for_status()
            created = response.json()
            if created.get("id") != event_id or created.get("status") == "cancelled":
                raise RuntimeError("Google did not verify event creation")
            return event_id
