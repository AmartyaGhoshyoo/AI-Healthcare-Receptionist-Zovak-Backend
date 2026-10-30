"""UNAUTHENTICATED development routes. Must NEVER be reachable in production."""
import html
import logging
import re
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from zovak.domain.schemas import ChatReply, ChatRequest, ConfirmReply, ConfirmRequest
from zovak.agents.memory import note_for_agent
from zovak.agents.runner import run_receptionist, stream_receptionist
from zovak.services.booking_state import booking_state
from zovak.services.mock_scheduler import SchedulingError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/dev", tags=["development ONLY"])


@router.post("/chat/stream")
async def chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:
    """Website chat: Nofa's reply as plain text, sent piece by piece as the AI writes it."""
    if not request.app.state.settings.openai_api_key:
        raise HTTPException(status_code=503, detail="Configure OPENAI_API_KEY locally first")

    async def words():
        try:
            async for piece in stream_receptionist(request.app.state.scheduler, body.tenant_id,
                                                   body.conversation_id, body.message):
                yield piece
        except Exception:
            logger.exception("Streamed chat turn failed")
            yield "Sorry, something went wrong on my side. Please try again."

    return StreamingResponse(words(), media_type="text/plain; charset=utf-8",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/chat", response_model=ChatReply)
async def chat(body: ChatRequest, request: Request) -> ChatReply:
    if not request.app.state.settings.openai_api_key:
        raise HTTPException(status_code=503, detail="Configure OPENAI_API_KEY locally first")
    reply = await run_receptionist(
        request.app.state.scheduler, body.tenant_id, body.conversation_id, body.message,
    )
    return ChatReply(response=reply, conversation_id=body.conversation_id)


@router.post("/confirm", response_model=ConfirmReply)
async def confirm(body: ConfirmRequest, request: Request) -> ConfirmReply:
    # Explicit confirmation is separate from LLM tools; no booking on tool hallucinations.
    try:
        reply = await request.app.state.scheduler.confirm(
            body.tenant_id, body.conversation_id, body.proposal_id
        )
    except SchedulingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await note_for_agent(body.tenant_id, body.conversation_id,
                         "The customer tapped Confirm booking. The appointment is now CONFIRMED.")
    return reply


# ── Booking card for website text chat ───────────────────────────────────────
# Same steps as the voice panel. In chat the customer types their email in the
# conversation, so the email box only appears if the agent proposed without one.

class ChatProposalEmail(BaseModel):
    tenant_id: str = "demo-business"  # DEVELOPMENT ONLY: NOT authenticated
    conversation_id: str = Field(min_length=1, max_length=120)
    proposal_id: str = Field(min_length=1, max_length=64)
    email: str | None = Field(default=None, max_length=254)


class ChatChange(BaseModel):
    tenant_id: str = "demo-business"  # DEVELOPMENT ONLY: NOT authenticated
    conversation_id: str = Field(min_length=1, max_length=120)
    change_id: str = Field(min_length=1, max_length=64)


@router.get("/booking-state")
async def chat_booking_state(conversation_id: str, request: Request,
                             tenant_id: str = "demo-business") -> dict:
    scheduler = request.app.state.scheduler
    try:
        pending = await scheduler.latest_pending(tenant_id, conversation_id)
    except SchedulingError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return await booking_state(
        scheduler, tenant_id, conversation_id, proposal=pending,
        email_ready=bool(pending and pending.customer_email),
        email_request_id=pending.id if pending else None,
    )


@router.post("/proposal-email")
async def chat_set_email(body: ChatProposalEmail, request: Request) -> dict:
    """Attach a typed email to the pending draft. Never books."""
    if not body.email:
        raise HTTPException(status_code=422, detail="Enter an email address")
    try:
        await request.app.state.scheduler.set_proposal_email(
            body.tenant_id, body.conversation_id, body.proposal_id, body.email
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SchedulingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await note_for_agent(body.tenant_id, body.conversation_id,
                         "The customer entered their email on screen for the pending appointment.")
    return {"status": "email_saved", "booking_confirmed": False}


@router.post("/proposal-email/clear")
async def chat_clear_email(body: ChatProposalEmail, request: Request) -> dict:
    """'Change email': remove the draft's email so the box shows again."""
    try:
        await request.app.state.scheduler.clear_proposal_email(
            body.tenant_id, body.conversation_id, body.proposal_id
        )
    except SchedulingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "email_cleared"}


@router.post("/change/confirm")
async def chat_confirm_change(body: ChatChange, request: Request) -> dict:
    """Explicit 'Confirm move' / 'Cancel appointment' tap; the agent cannot do this."""
    try:
        result = await request.app.state.scheduler.apply_change(
            body.tenant_id, body.conversation_id, body.change_id
        )
    except SchedulingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await note_for_agent(body.tenant_id, body.conversation_id, (
        "The customer confirmed the move; the booking is now at the new time."
        if result.status == "moved" else
        "The customer confirmed the cancellation; the booking is cancelled."
    ))
    return result.model_dump()


@router.post("/change/discard")
async def chat_discard_change(body: ChatChange, request: Request) -> dict:
    try:
        await request.app.state.scheduler.discard_change(
            body.tenant_id, body.conversation_id, body.change_id
        )
    except SchedulingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await note_for_agent(body.tenant_id, body.conversation_id,
                         "The customer chose to keep the appointment unchanged.")
    return {"status": "kept"}


@router.get("/sms-outbox", response_class=HTMLResponse)
async def sms_outbox(request: Request) -> HTMLResponse:
    """Texts that would have been sent (free testing without an SMS provider)."""
    messages = list(reversed(request.app.state.sms.messages))
    rows = "".join(
        f"<li><b>To {html.escape(m.to)}</b> · {m.sent_at:%H:%M:%S} UTC<br>"
        + re.sub(r"(https?://\S+)", r'<a href="\1">\1</a>', html.escape(m.body))
        + "</li>"
        for m in messages
    ) or "<li>No texts yet.</li>"
    return HTMLResponse(
        "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<meta http-equiv=refresh content=5><title>SMS test inbox</title>"
        "<body style='font:16px/1.5 system-ui;max-width:640px;margin:24px auto;padding:0 16px'>"
        f"<h1>SMS test inbox</h1><p>Refreshes every 5 seconds.</p><ul>{rows}</ul>"
    )
