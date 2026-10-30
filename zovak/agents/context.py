from collections.abc import Callable
from dataclasses import dataclass
from zovak.services.mock_scheduler import DemoScheduler


@dataclass(frozen=True)
class AgentContext:
    tenant_id: str
    conversation_id: str
    scheduler: DemoScheduler
    email_via_form: bool = False  # Voice web UI; text/chat behavior unchanged.
    on_off_topic: Callable[[], object] | None = None  # voice calls count strikes; chat doesn't
    on_end_call: Callable[[], object] | None = None  # caller is finished (voice only)
    on_misuse: Callable[[], object] | None = None  # caller is clearly wasting time (voice only)
    channel: str = "chat"  # "chat", "web" (website voice), "phone" or "whatsapp"
    customer_phone: str | None = None  # caller/WhatsApp number, kept on bookings for delay updates
