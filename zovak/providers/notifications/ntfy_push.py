"""Free push notifications via ntfy (https://ntfy.sh) — a TESTING stand-in for SMS.

Each text Nofa would send pops up on your phone in the ntfy app, and tapping it
opens the form link. Anyone who knows the topic name can read it, so use a long
random topic and never use this for real customers.
"""
import re

import httpx

_LINK = re.compile(r"https?://\S+")


class NtfyPush:
    def __init__(self, topic: str, server: str = "https://ntfy.sh", *,
                 client: httpx.AsyncClient | None = None):
        self.url = f"{server.rstrip('/')}/{topic}"
        self.client = client

    async def send(self, to: str, body: str) -> str:
        headers = {"Title": f"Nofa SMS to {to}", "Tags": "calendar"}
        link = _LINK.search(body)
        if link:
            headers["Click"] = link.group(0)  # tapping the notification opens the form
        if self.client is None:
            async with httpx.AsyncClient(timeout=10) as http:
                response = await http.post(self.url, content=body.encode(), headers=headers)
        else:
            response = await self.client.post(self.url, content=body.encode(), headers=headers)
        response.raise_for_status()
        return str(response.json().get("id", ""))
