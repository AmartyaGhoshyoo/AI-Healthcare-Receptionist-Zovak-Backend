from datetime import datetime, timezone
import httpx
import pytest
from zovak.providers.calendar.google import GoogleCalendar


@pytest.mark.asyncio
async def test_google_freebusy_and_insert_using_mock_transport():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer fake-token"
        if request.url.path.endswith("/freeBusy"):
            return httpx.Response(200, json={"calendars": {"primary": {"busy": []}}})
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"id": "abcd1234", "status": "confirmed"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleCalendar("fake-token", "primary", client=client)
        start = datetime(2027, 1, 1, 10, tzinfo=timezone.utc)
        end = datetime(2027, 1, 1, 11, tzinfo=timezone.utc)
        assert await adapter.is_available(start, end)
        assert await adapter.create_event("Meeting", start, end, "abcd1234") == "abcd1234"
    assert len(requests) == 2
