"""WhatsApp Cloud API (Meta) client: text, tap-button messages, read receipts, call actions.

Free testing works with Meta's test number (up to 5 registered recipients).
Business-started messages outside the 24-hour customer-service window need
approved templates; replies inside the window (a chatbot) don't.
"""
from __future__ import annotations

import httpx


class WhatsAppCloud:
    def __init__(self, access_token: str, phone_number_id: str, *, graph_version: str = "v25.0",
                 client: httpx.AsyncClient | None = None):
        self.base = f"https://graph.facebook.com/{graph_version}/{phone_number_id}"
        self.headers = {"Authorization": f"Bearer {access_token}"}
        self.client = client

    async def _post(self, path: str, payload: dict) -> dict:
        request = {"url": f"{self.base}/{path}", "headers": self.headers, "json": payload}
        if self.client is None:
            async with httpx.AsyncClient(timeout=15) as http:
                response = await http.post(**request)
        else:
            response = await self.client.post(**request)
        data = response.json() if response.content else {}
        if response.status_code >= 400:
            error = data.get("error", {})
            raise RuntimeError(f"WhatsApp API {error.get('code', response.status_code)}: "
                               f"{error.get('message', response.text[:200])}")
        return data

    async def send_text(self, to: str, body: str) -> str:
        data = await self._post("messages", {
            "messaging_product": "whatsapp", "to": to, "type": "text",
            "text": {"body": body[:4096]},
        })
        return str(data["messages"][0]["id"])

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> str:
        """Up to 3 tap buttons: (id, title), titles at most 20 characters."""
        data = await self._post("messages", {
            "messaging_product": "whatsapp", "to": to, "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body[:1024]},
                "action": {"buttons": [
                    {"type": "reply", "reply": {"id": bid, "title": title[:20]}}
                    for bid, title in buttons[:3]
                ]},
            },
        })
        return str(data["messages"][0]["id"])

    async def mark_read(self, message_id: str, *, typing: bool = False) -> None:
        """Blue ticks; with typing=True also shows "typing…" (up to 25 s or until we reply)."""
        payload = {"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
        if typing:
            payload["typing_indicator"] = {"type": "text"}
        await self._post("messages", payload)

    async def call_action(self, call_id: str, action: str, sdp: str | None = None) -> None:
        """pre_accept / accept (with our SDP answer), reject or terminate a WhatsApp call."""
        payload: dict = {"messaging_product": "whatsapp", "call_id": call_id, "action": action}
        if sdp is not None:
            payload["session"] = {"sdp_type": "answer", "sdp": sdp}
        await self._post("calls", payload)
