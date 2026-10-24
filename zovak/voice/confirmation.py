"""Local voice booking confirmation gate.

This deterministic guard sits OUTSIDE the LLM. An LLM's claim that a
customer confirmed is never sufficient to commit an appointment.

Prototype limitation: input comes from GPT-Live transcript fragments, which
are not a certified, final or authenticated record of user intent. Do not use
this module as the sole confirmation control for real customer bookings.
"""
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Literal
from zoneinfo import ZoneInfo

from zovak.domain.schemas import BookingProposal

Decision = Literal["approve", "decline", "revise", "unclear"]


def appointment_details(proposal, service_name):
    start = proposal.start.astimezone(ZoneInfo("Asia/Dhaka"))
    end = proposal.end.astimezone(ZoneInfo("Asia/Dhaka"))

    duration = int(
        (proposal.end - proposal.start).total_seconds() / 60
    )

    return (
        f"Customer name: {proposal.customer_name}. "
        f"Service: {service_name}. "
        f"Date: {start.strftime('%B %d, %Y')}. "
        f"Time: {start.strftime('%I:%M %p')} to "
        f"{end.strftime('%I:%M %p')}, Dhaka time. "
        f"Duration: {duration} minutes."
    )

def normalize_utterance(text: str) -> str:
    text = text.casefold().replace("’", "'")
    text = re.sub(r"[^\w\s']+", " ", text)
    return " ".join(text.split())


# These phrases are deliberately strict. Bare "yes" is NOT sufficient:
# it could be only the first transcript fragment of "yes, but change it".
_APPROVAL = re.compile(
    r"^(?:(?:yes|yeah|yep|yup|sure|absolutely|certainly|of course|"
    r"i am sure|i'm sure)\s+)?"
    r"(?:please\s+)?"
    r"(?:confirm|book|finalize|schedule)\s+"
    r"(?:(?:my|the|that|this)\s+)?"
    r"(?:appointment|booking|reservation|it|that)"
    r"(?:\s+please)?$"
)
_GO_AHEAD = re.compile(
    r"^(?:(?:yes|yeah|sure)\s+)?(?:please\s+)?go ahead and "
    r"(?:confirm|book|finalize)\s+(?:(?:my|the|that)\s+)?"
    r"(?:appointment|booking|it|that)(?:\s+please)?$"
)
_DECLINE = re.compile(
    r"^(?:no|no thanks|no thank you|no please|no not yet|not yet|"
    r"don't confirm|do not confirm|please don't confirm|cancel|cancel it|"
    r"stop|stop please|i don't want to confirm|i do not want to confirm)$"
)
_REVISION = re.compile(
    r"\b(?:change|different|instead|actually|reschedule|another time|"
    r"make it|move it|wait|hold on)\b"
)


def classify_confirmation(text: str) -> Decision:
    """Approve only an *entire*, unmistakably positive utterance."""
    utterance = normalize_utterance(text)
    if _DECLINE.fullmatch(utterance):
        return "decline"
    if _REVISION.search(utterance):
        return "revise"
    if _APPROVAL.fullmatch(utterance) or _GO_AHEAD.fullmatch(utterance):
        return "approve"
    return "unclear"


@dataclass
class VoiceConfirmationGate:
    proposal_id: str | None = None

    def arm(self, proposal_id: str) -> None:
        self.proposal_id = proposal_id

    def clear(self) -> None:
        self.proposal_id = None



def polite_confirmation_question(proposal, service_name):
    details = appointment_details(proposal, service_name)

    return (
        f"Let me read back your appointment details. "
        f"{details} "
        "Your appointment is not confirmed yet. "
        "Are you sure you would like me to confirm it?"
    )

