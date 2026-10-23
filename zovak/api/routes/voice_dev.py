"""Unauthenticated LOCAL development WebRTC voice demo. Do not expose publicly."""
import asyncio
import logging
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from openai import APIError, AsyncOpenAI
from pydantic import BaseModel, Field

from zovak.providers.voice.gpt_live import LIVE_PROMPT
from zovak.voice.sideband import VoiceSideband

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/dev/voice", tags=["development ONLY — voice"])
_PAGE = Path(__file__).resolve().parents[2] / "voice" / "static" / "index.html"


class SDPOffer(BaseModel):
    sdp: str = Field(min_length=1, max_length=80_000)


@router.get("")
async def voice_demo() -> FileResponse:
    return FileResponse(_PAGE, media_type="text/html")


@router.post("/session", status_code=201)
async def create_voice_session(body: SDPOffer, request: Request) -> dict:
    settings = request.app.state.settings
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="Set OPENAI_API_KEY in .env")
    # This URL is development-only and is registered only when ENABLE_DEV_ROUTES=true.
    try:
        async with AsyncOpenAI(api_key=settings.openai_api_key, max_retries=0) as client:
            result = await client.live.create(
                session={
                    "model": "gpt-live-1",
                    "instructions": LIVE_PROMPT + " This is a local scheduling DEMO. Only say a booking is confirmed if verified by the backend. Avoid speaking proposal IDs aloud; let the screen display them.",
                    "delegation": {"type": "client"},
                },
                transport={"type": "webrtc", "sdp": body.sdp},
            )
    except (APIError, AttributeError) as exc:
        logger.exception("Could not create GPT-Live session")
        raise HTTPException(
            status_code=getattr(exc, "status_code", None) or 502,
            detail="GPT-Live session failed. Check API model access, billing and SDK version.",
        ) from exc

    conversation_id = f"voice-{uuid4().hex}"
    session_id = result.session.id
    if not hasattr(request.app.state, "voice_sidebands"):
        request.app.state.voice_sidebands = {}
    active: dict = request.app.state.voice_sidebands
    handler = VoiceSideband(
        session_id=session_id,
        conversation_id=conversation_id,
        api_key=settings.openai_api_key,
        scheduler=request.app.state.scheduler,
        agent_model=settings.openai_agent_model,
        on_close=lambda sid: active.pop(sid, None),
    )
    active[session_id] = handler
    handler.start()
    try:
        await asyncio.wait_for(handler.ready.wait(), timeout=10)
    except asyncio.TimeoutError as exc:
        await handler.stop()
        raise HTTPException(status_code=502, detail="GPT-Live sideband did not connect") from exc
    if handler.failed or handler.closed:
        await handler.stop()
        raise HTTPException(status_code=502, detail="GPT-Live sideband could not attach; check server logs")

    return {
        "session": {"id": session_id},
        "transport": {"type": "webrtc", "sdp": result.transport.sdp},
        "conversation_id": conversation_id,
    }


@router.get("/proposals")
async def proposals(conversation_id: str, request: Request) -> dict:
    # Development-only endpoint to show *actual* backend proposal records.
    scheduler = request.app.state.scheduler
    return {"proposals": [
        {"proposal_id": p.id, "customer_name": p.customer_name,
         "service_id": p.service_id, "start": p.start.isoformat(), "status": p.status}
        for p in scheduler._proposals.values()
        if p.tenant_id == "demo-business" and p.conversation_id == conversation_id
    ]}
