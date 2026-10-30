"""Run one receptionist turn for a text conversation (website chat, WhatsApp)."""
from __future__ import annotations

from collections.abc import AsyncIterator

from zovak.agents.memory import memory_session
from zovak.services.booking_state import booking_status_for_agent
from zovak.services.doctor_choice import read_choices


async def _prepare(scheduler, tenant_id: str, conversation_id: str, message: str, channel: str,
                   customer_phone: str | None):
    """Plain-code choices first, then the agent with the live status and this chat's memory."""
    from zovak.agents.context import AgentContext
    from zovak.agents.receptionist import make_receptionist

    await read_choices(scheduler, tenant_id, conversation_id, message)
    context = AgentContext(tenant_id=tenant_id, conversation_id=conversation_id,
                           scheduler=scheduler, channel=channel, customer_phone=customer_phone)
    status = await booking_status_for_agent(scheduler, tenant_id, conversation_id, channel)
    return make_receptionist(booking_status=status, channel=channel), context


async def run_receptionist(scheduler, tenant_id: str, conversation_id: str, message: str, *,
                           channel: str = "chat", customer_phone: str | None = None) -> str:
    from agents import Runner

    agent, context = await _prepare(scheduler, tenant_id, conversation_id, message, channel, customer_phone)
    session = memory_session(tenant_id, conversation_id)
    try:
        result = await Runner.run(agent, message, context=context, session=session)
        return str(result.final_output)
    finally:
        await session.close()


async def stream_receptionist(scheduler, tenant_id: str, conversation_id: str, message: str, *,
                              channel: str = "chat") -> AsyncIterator[str]:
    """The same turn, yielding Nofa's words as the AI writes them (tools run first, unseen)."""
    from agents import Runner
    from openai.types.responses import ResponseTextDeltaEvent

    agent, context = await _prepare(scheduler, tenant_id, conversation_id, message, channel, None)
    session = memory_session(tenant_id, conversation_id)
    try:
        result = Runner.run_streamed(agent, message, context=context, session=session)
        async for event in result.stream_events():
            if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
                yield event.data.delta
    finally:
        await session.close()
