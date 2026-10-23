"""Accumulate GPT-Live input transcript fragments for local client delegation.

Transcript deltas are fragments, not complete turns. Timing is approximate. This
simple buffer is intentionally not a production-grade turn-boundary algorithm.
"""
from dataclasses import dataclass, field


@dataclass
class TranscriptBuffer:
    # Each entry is (end_ms, text), in receive order.
    input_fragments: list[tuple[int, str]] = field(default_factory=list)
    last_delegation_ms: int = -1

    def ingest(self, event: dict) -> None:
        if event.get("type") != "session.input_transcript.delta":
            return
        text = event.get("delta")
        if not isinstance(text, str) or not text:
            return
        end_ms = event.get("end_ms")
        if not isinstance(end_ms, (int, float)):
            end_ms = self.input_fragments[-1][0] if self.input_fragments else 0
        self.input_fragments.append((int(end_ms), text))
        if len(self.input_fragments) > 3000:
            self.input_fragments = self.input_fragments[-3000:]

    def take_for_delegation(self, offset_ms: int) -> str:
        # Include only the user's fragments since our last delegated request.
        # A 250ms allowance reduces the chance of excluding late fragments.
        max_ms = offset_ms + 250
        selected = [
            text for end_ms, text in self.input_fragments
            if self.last_delegation_ms < end_ms <= max_ms
        ]
        self.last_delegation_ms = max(self.last_delegation_ms, offset_ms)
        return "".join(selected).strip()
