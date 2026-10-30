import pytest

import zovak.agents.memory as memory


@pytest.fixture(autouse=True)
def isolated_agent_memory(tmp_path, monkeypatch):
    """Keep tests from writing into the real .data/dev_conversations.db."""
    monkeypatch.setattr(memory, "MEMORY_DB", tmp_path / "agent_memory.db")
