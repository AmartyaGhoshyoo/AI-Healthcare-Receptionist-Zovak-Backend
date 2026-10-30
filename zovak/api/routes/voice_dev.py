"""Unauthenticated LOCAL development WebRTC voice demo. Do not expose publicly."""
import asyncio
import logging
from secrets import compare_digest
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse
from openai import APIError, AsyncOpenAI
from pydantic import BaseModel, Field

from zovak.agents.receptionist import today_instructions
from zovak.providers.voice.gpt_live import LIVE_PROMPT
from zovak.voice.sideband import VoiceSideband
from zovak.services.mock_scheduler import SchedulingError

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
                    "instructions": LIVE_PROMPT + " " + today_instructions() + " This is a local scheduling DEMO. Only say a booking is confirmed if verified by the backend. Avoid speaking proposal IDs aloud; let the screen display them.",
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
        "ui_token": handler.ui_token,
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


class EmailInputSubmission(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=120)
    request_id: str = Field(min_length=1, max_length=128)
    email: str = Field(min_length=3, max_length=254)


def _get_authorized_voice(request: Request, conversation_id: str, authorization: str | None):
    # LOCAL DEV ONLY. For production use signed-in users and tenant/session auth.
    handlers = getattr(request.app.state, "voice_sidebands", {})
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, detail="Voice session token required")
    token = authorization[7:]
    for handler in handlers.values():
        if (handler.conversation_id == conversation_id and not handler.closed
                and compare_digest(handler.ui_token, token)):
            return handler
    raise HTTPException(403, detail="Invalid or expired voice session")


@router.get("/email-input")
async def get_email_input(conversation_id: str, request: Request,
                          authorization: str | None = Header(default=None)) -> dict:
    """Poll while calling. Show a box when state is email_required."""
    handler = _get_authorized_voice(request, conversation_id, authorization)
    return await handler.email_state()


@router.post("/email-input")
async def post_email_input(body: EmailInputSubmission, request: Request,
                           authorization: str | None = Header(default=None)) -> dict:
    """Confirm only the typed email. DOES NOT create or confirm a booking."""
    handler = _get_authorized_voice(request, body.conversation_id, authorization)
    try:
        return await handler.submit_email(request_id=body.request_id, email=body.email)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except SchedulingError as exc:
        raise HTTPException(409, detail=str(exc)) from exc


# ── Booking card for the website voice panel ─────────────────────────────────
# All actions need the call's ui_token and re-check server state; none of them
# trusts IDs beyond matching the single pending item for this call.

class ProposalAction(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=120)
    proposal_id: str = Field(min_length=1, max_length=64)


class ChangeAction(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=120)
    change_id: str = Field(min_length=1, max_length=64)


class SlotPick(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=120)
    start: str = Field(min_length=10, max_length=40)


async def _run(action):
    try:
        return await action
    except SchedulingError as exc:
        raise HTTPException(409, detail=str(exc)) from exc


@router.get("/booking-state")
async def get_booking_state(conversation_id: str, request: Request,
                            authorization: str | None = Header(default=None)) -> dict:
    """Polled during the call: email box, confirm, booked, move or cancel card."""
    handler = _get_authorized_voice(request, conversation_id, authorization)
    return await handler.booking_state()


@router.post("/confirm-booking")
async def post_confirm_booking(body: ProposalAction, request: Request,
                               authorization: str | None = Header(default=None)) -> dict:
    handler = _get_authorized_voice(request, body.conversation_id, authorization)
    return await _run(handler.confirm_booking(proposal_id=body.proposal_id))


@router.post("/reopen-email")
async def post_reopen_email(body: ProposalAction, request: Request,
                            authorization: str | None = Header(default=None)) -> dict:
    handler = _get_authorized_voice(request, body.conversation_id, authorization)
    return await _run(handler.reopen_email(proposal_id=body.proposal_id))


@router.post("/pick-slot")
async def post_pick_slot(body: SlotPick, request: Request,
                         authorization: str | None = Header(default=None)) -> dict:
    """A time button: Nofa hears it as the caller choosing that time."""
    handler = _get_authorized_voice(request, body.conversation_id, authorization)
    return await _run(handler.pick_slot(start=body.start))


@router.post("/change/confirm")
async def post_confirm_change(body: ChangeAction, request: Request,
                              authorization: str | None = Header(default=None)) -> dict:
    handler = _get_authorized_voice(request, body.conversation_id, authorization)
    return await _run(handler.confirm_change(change_id=body.change_id))


@router.post("/change/discard")
async def post_discard_change(body: ChangeAction, request: Request,
                              authorization: str | None = Header(default=None)) -> dict:
    handler = _get_authorized_voice(request, body.conversation_id, authorization)
    return await _run(handler.discard_change(change_id=body.change_id))
