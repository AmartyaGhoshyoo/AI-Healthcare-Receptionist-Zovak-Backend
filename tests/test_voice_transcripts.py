from zovak.voice.transcripts import TranscriptBuffer


def test_transcript_fragments_are_collected_since_last_delegation():
    b = TranscriptBuffer()
    b.ingest({"type": "session.input_transcript.delta", "end_ms": 100, "delta": "Book "})
    b.ingest({"type": "session.input_transcript.delta", "end_ms": 200, "delta": "Monday"})
    assert b.take_for_delegation(300) == "Book Monday"
    b.ingest({"type": "session.input_transcript.delta", "end_ms": 500, "delta": "Actually Tuesday"})
    assert b.take_for_delegation(600) == "Actually Tuesday"


def test_non_input_and_empty_fragments_are_ignored():
    b = TranscriptBuffer()
    b.ingest({"type": "session.output_transcript.delta", "end_ms": 100, "delta": "Hello"})
    b.ingest({"type": "session.input_transcript.delta", "end_ms": 100, "delta": ""})
    assert b.take_for_delegation(200) == ""
