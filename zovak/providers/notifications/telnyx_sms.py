"""Async Telnyx SMS adapter (Messaging API v2).

Bangladesh needs a registered alphanumeric sender for Telnyx, so +880 numbers
are routed elsewhere (see SmsRouter). Sweden worked with the profile's
unregistered alpha sender. Carriers in some countries filter links.
"""
import httpx


class TelnyxSms:
    def __init__(self, api_key: str, from_number: str, messaging_profile_id: str = "", *,
                 client: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self.from_number = from_number
        self.messaging_profile_id = messaging_profile_id
        self.client = client

    async def send(self, to: str, body: str) -> str:
        payload = {"from": self.from_number, "to": to, "text": body}
        if self.messaging_profile_id:
            payload["messaging_profile_id"] = self.messaging_profile_id
        request = {
            "url": "https://api.telnyx.com/v2/messages",
            "headers": {"Authorization": f"Bearer {self.api_key}"},
            "json": payload,
        }
        if self.client is None:
            async with httpx.AsyncClient(timeout=15) as http:
                response = await http.post(**request)
        else:
            response = await self.client.post(**request)
        data = response.json() if response.content else {}
        if response.status_code >= 400 or data.get("errors"):
            error = (data.get("errors") or [{}])[0]
            raise RuntimeError(f"Telnyx {error.get('code', response.status_code)}: "
                               f"{error.get('detail') or error.get('title') or response.text[:200]}")
        return str(data["data"]["id"])
