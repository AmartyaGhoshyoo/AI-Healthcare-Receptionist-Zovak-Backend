"""The agent's per-conversation memory, and notes about things that happened outside it.

Buttons on the website (and strict spoken confirmations) change bookings
without the agent taking part. Without a note, the agent's memory would still
say "pending" and it could give the customer wrong answers.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

MEMORY_DB = Path(".data") / "dev_conversations.db"


def memory_session(tenant_id: str, conversation_id: str):
    from agents.extensions.memory import AsyncSQLiteSession

    MEMORY_DB.parent.mkdir(exist_ok=True)
    return AsyncSQLiteSession(f"{tenant_id}:{conversation_id}", db_path=str(MEMORY_DB))


async def note_for_agent(tenant_id: str, conversation_id: str, text: str) -> None:
    """Record a booking-system event so the agent's next turn knows about it."""
    session = memory_session(tenant_id, conversation_id)
    try:
        await session.add_items([{"role": "system", "content": f"Booking system update: {text}"}])
    except Exception:
        logger.warning("Could not add a booking note to agent memory", exc_info=True)
    finally:
        await session.close()
