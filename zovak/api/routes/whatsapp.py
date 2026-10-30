"""WhatsApp webhook (Meta Cloud API): verification, signed messages and calls.

Public through the tunnel. Every POST must carry Meta's X-Hub-Signature-256
(HMAC-SHA256 of the raw body with the app secret). We answer 200 at once and
process in the background, because Meta retries slow or failed deliveries.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/whatsapp", tags=["whatsapp (public webhook)"])
_tasks: set[asyncio.Task] = set()


@router.get("/webhook", response_class=PlainTextResponse)
async def verify(request: Request) -> str:
    """Meta's one-time check when you save the webhook URL in the app dashboard."""
    params = request.query_params
    expected = request.app.state.settings.whatsapp_verify_token
    if (expected and params.get("hub.mode") == "subscribe"
            and hmac.compare_digest(params.get("hub.verify_token", ""), expected)):
        return params.get("hub.challenge", "")
    raise HTTPException(403, detail="Verification failed")


def signature_ok(body: bytes, header: str, app_secret: str) -> bool:
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256="), expected)


@router.post("/webhook")
async def receive(request: Request) -> dict:
    settings = request.app.state.settings
    bot = getattr(request.app.state, "whatsapp", None)
    if bot is None or not settings.whatsapp_app_secret:
        raise HTTPException(503, detail="WhatsApp is not configured in .env")
    body = await request.body()
    if not signature_ok(body, request.headers.get("x-hub-signature-256", ""), settings.whatsapp_app_secret):
        raise HTTPException(401, detail="Invalid signature")
    payload = json.loads(body)
    handlers = [bot.handle_payload(payload)]
    calls = getattr(request.app.state, "whatsapp_calls", None)
    if calls is not None:
        handlers.append(calls.handle_payload(payload))
    for work in handlers:
        task = asyncio.create_task(work)
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    return {"ok": True}
