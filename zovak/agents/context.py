from dataclasses import dataclass
from zovak.services.mock_scheduler import DemoScheduler


@dataclass(frozen=True)
class AgentContext:
    tenant_id: str
    conversation_id: str
    scheduler: DemoScheduler
    email_via_form: bool = False  # Voice web UI; text/chat behavior unchanged.
