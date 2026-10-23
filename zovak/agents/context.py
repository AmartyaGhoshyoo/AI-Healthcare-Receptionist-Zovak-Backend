from dataclasses import dataclass
from zovak.services.mock_scheduler import DemoScheduler


@dataclass(frozen=True)
class AgentContext:
    tenant_id: str
    conversation_id: str
    scheduler: DemoScheduler
