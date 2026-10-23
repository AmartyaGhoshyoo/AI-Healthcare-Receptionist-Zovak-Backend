"""UNAUTHENTICATED development routes. Must NEVER be reachable in production."""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from zovak.domain.schemas import ChatReply, ChatRequest, ConfirmReply, ConfirmRequest
from zovak.services.mock_scheduler import SchedulingError

router = APIRouter(prefix="/api/v1/dev", tags=["development ONLY"])


@router.post("/chat", response_model=ChatReply)
async def chat(body: ChatRequest, request: Request) -> ChatReply:
    if not request.app.state.settings.openai_api_key:
        raise HTTPException(status_code=503, detail="Configure OPENAI_API_KEY locally first")
    from agents import Runner
    from agents.extensions.memory import AsyncSQLiteSession
    from zovak.agents.context import AgentContext
    from zovak.agents.receptionist import make_receptionist

    folder = Path(".data")
    folder.mkdir(exist_ok=True)
    session_id = f"{body.tenant_id}:{body.conversation_id}"
    session = AsyncSQLiteSession(session_id, db_path=str(folder / "dev_conversations.db"))
    context = AgentContext(
        tenant_id=body.tenant_id,
        conversation_id=body.conversation_id,
        scheduler=request.app.state.scheduler,
    )
    result = await Runner.run(
        make_receptionist(), body.message, context=context, session=session,
    )
    return ChatReply(response=str(result.final_output), conversation_id=body.conversation_id)


@router.post("/confirm", response_model=ConfirmReply)
async def confirm(body: ConfirmRequest, request: Request) -> ConfirmReply:
    # Explicit confirmation is separate from LLM tools; no booking on tool hallucinations.
    try:
        return await request.app.state.scheduler.confirm(
            body.tenant_id, body.conversation_id, body.proposal_id
        )
    except SchedulingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
