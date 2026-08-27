from __future__ import annotations

from pathlib import Path

import pytest

from lune.memory.store import MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    instance = MemoryStore(tmp_path / "private" / "lune.sqlite3")
    yield instance
    instance.close()


def complete_turn(
    store: MemoryStore,
    session_id: str,
    generation_id: int,
    *,
    user: str | None = None,
    assistant: str | None = None,
) -> str:
    turn_id = store.begin_turn(session_id, generation_id)
    store.accept_user_transcript(turn_id, user or f"user-{generation_id}")
    store.append_assistant_playback(turn_id, assistant or f"assistant-{generation_id}")
    store.complete_turn(turn_id)
    return turn_id
