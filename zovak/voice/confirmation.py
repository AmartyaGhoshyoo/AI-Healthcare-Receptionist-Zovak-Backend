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


# Spoken approvals: natural wording is fine ("uh yes, confirm it now please"),
# but speech arrives in pieces, so a reply that could be the START of
# "yes, but change the time" must never approve. Three rules guard that:
#   1. a bare "yes" is not enough: there must be an action ("confirm", "book",
#      "go ahead"...);
#   2. any hedge or change word ("but", "wait", "change", "not"...) blocks it;
#   3. a reply that ends mid-phrase ("confirm my") is unfinished, not a yes.
# The sideband adds a 4th: it waits for a moment of silence before acting.
_FILLER = {"uh", "um", "uhm", "umm", "er", "erm", "ah", "oh", "okay", "ok", "well", "so",
           "now", "then", "just", "alright", "right", "great", "perfect", "fine", "good",
           "please", "thanks", "cool", "that's", "it's", "sounds", "looks"}
_HEDGE = {"but", "wait", "hold", "maybe", "perhaps", "if", "unless", "except", "not", "don't",
          "dont", "no", "never", "later", "actually", "instead", "change", "different",
          "another", "other", "reschedule", "think", "what", "how", "why", "when", "which",
          "or", "first", "before", "after", "tomorrow", "today", "pm", "am"}
_DANGLING = {"my", "the", "this", "a", "an", "to", "and", "for", "at", "on", "with", "in",
             "of", "your", "is", "can", "could", "would", "will", "you"}
_AFFIRM = (r"(?:yes|yeah|yep|yup|sure|absolutely|certainly|of course|correct|"
           r"that's right|that's correct|that's fine|sounds good|looks good|i'm sure|i am sure)")


def _approval(verbs: str, objects: str) -> re.Pattern:
    return re.compile(
        rf"^(?:{_AFFIRM}\s+)*(?:(?:can|could|would|will) you\s+)?"
        rf"(?:(?:go ahead|go for it|do it|let's do it)(?:\s+and)?\s*)?"
        rf"(?:(?:{verbs})(?:\s+(?:my|the|this|that))?(?:\s+(?:{objects}))?(?:\s+for me)?)?$"
    )


_BOOK_APPROVAL = _approval("confirm|book|finalize|schedule|lock in",
                           "appointment|booking|reservation|slot|it|that|one")
_MOVE_APPROVAL = _approval("confirm|move|do",
                           "move|change|appointment|new time|it|that")
_CANCEL_APPROVAL = _approval("confirm|cancel|do",
                             "cancellation|appointment|booking|it|that")


def _is_approval(text: str, pattern: re.Pattern) -> bool:
    words = normalize_utterance(text.replace("thank you", "thanks")).split()
    if not words or any(w in _HEDGE for w in words):
        return False
    words = [w for w in words if w not in _FILLER]
    if not words or words[-1] in _DANGLING:
        return False
    utterance = " ".join(words)
    # An action is required: affirmation words alone ("yes", "sure") don't count.
    if re.fullmatch(rf"(?:{_AFFIRM}\s*)+", utterance):
        return False
    return bool(pattern.fullmatch(utterance))


def is_approval(text: str, kind: str = "book") -> bool:
    """True only for a complete, unconditional yes to the pending booking/move/cancel."""
    pattern = {"book": _BOOK_APPROVAL, "move": _MOVE_APPROVAL, "cancel": _CANCEL_APPROVAL}[kind]
    return _is_approval(text, pattern)


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
    if is_approval(text, "book"):
        return "approve"
    return "unclear"


# Spoken replies while a move or cancellation of a confirmed booking is pending.
_KEEP = re.compile(
    r"^(?:no|no thanks|no thank you|not yet|never mind|nevermind|"
    r"(?:no\s+)?(?:please\s+)?keep (?:it|my appointment|the appointment|the original|the current time)|"
    r"don't (?:cancel|move) it|do not (?:cancel|move) it)$"
)

# "No, that's all" after a booking is done: the caller is finished.
_DONE = re.compile(
    r"^(?:no|nope|nah)?\s*(?:no\s+)?(?:thanks|thank you)?\s*"
    r"(?:that's all|that is all|that's it|that is it|nothing else|nothing|all good|i'm good|"
    r"i'm done|we're done|no more|bye|goodbye|bye bye|see you)?\s*(?:thanks|thank you|bye|goodbye)?$"
)


# Sounds that keep a line open without saying anything: fillers, coughs and the
# tags transcription adds for them ("[cough]", "(sniffs)", "*breathing*").
_NOISE_WORDS = {
    "uh", "um", "umm", "uhm", "hmm", "hm", "mm", "mmm", "mhm", "ah", "ahh", "oh", "er", "erm",
    "eh", "huh", "ha", "haha", "hah", "cough", "coughs", "coughing", "sniff", "sniffs",
    "sniffing", "sneeze", "sneezes", "breath", "breathing", "breathes", "sigh", "sighs",
    "laugh", "laughs", "laughter", "noise", "inaudible", "silence", "static", "music",
    "background", "unintelligible", "clears", "throat",
}
_NOISE_TAG = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*")


def is_meaningful(text: str) -> bool:
    """True if the caller said real words, not just a cough, "uh" or "hmm".

    Works for any language: any word of 2+ letters (or a number) that isn't a
    known filler or noise counts.
    """
    words = re.findall(r"[^\W\d_]+|\d+", _NOISE_TAG.sub(" ", text).casefold())
    return any(w.isdigit() or (len(w) >= 2 and w not in _NOISE_WORDS) for w in words)


def is_done(text: str) -> bool:
    """The caller has nothing more to do ("no, that's all", "bye")."""
    utterance = normalize_utterance(text)
    return bool(utterance) and bool(_DONE.fullmatch(utterance))


# A caller who clearly wants to finish, at any point in the call. Plain code, so the
# call ends even when the voice model answers "bye" itself instead of asking ZOVAK.
_GOODBYE = re.compile(
    r"\b(?:bye|goodbye|bye bye|see you|see ya|talk (?:to you )?later|take care|"
    r"that's all|that is all|that's it|that is it|nothing else|i'm done|i am done|we're done|"
    r"i (?:have|need|got) to go|i gotta go|gotta go|i'll call (?:you )?(?:back|later)|"
    r"(?:end|cut|finish|stop|close|disconnect) (?:the |this )?call|hang up|you can hang up)\b"
)
# ...unless the same words still ask for something ("bye, but first move it").
_STILL_ASKING = re.compile(
    r"\b(?:book|move|cancel|change|reschedule|appointment for|available|free|when|what|which|"
    r"how|can you|could you|but|wait|before|first|actually|doctor|dr)\b"
)


def is_goodbye(text: str) -> bool:
    """The caller clearly wants to end the call ("thanks, bye", "I have to go", "hang up")."""
    utterance = normalize_utterance(text)
    if not utterance or len(utterance.split()) > 12 or "?" in text:
        return False
    return bool(_GOODBYE.search(utterance)) and not _STILL_ASKING.search(utterance)


def classify_change_reply(text: str, kind: str) -> Decision:
    """Classify a reply to 'shall I move/cancel your confirmed appointment?'."""
    utterance = normalize_utterance(text)
    if _KEEP.fullmatch(utterance):
        return "decline"
    if is_approval(text, kind):
        return "approve"
    if _REVISION.search(utterance):
        return "revise"
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

