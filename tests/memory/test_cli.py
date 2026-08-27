from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from lune.cli import build_parser, main
from lune.memory.embedding import E5_MODEL_ID, E5_MODEL_REVISION
from lune.memory.store import EMBEDDING_DIMENSIONS, MemoryStore
from lune.paths import LunePaths
from tests.memory.conftest import complete_turn


def test_memory_cli_has_no_bulk_clear_and_search_takes_no_query_argument() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["memory", "clear"])
    with pytest.raises(SystemExit):
        parser.parse_args(["memory", "search", "private-query"])


def test_forget_requires_exact_id_confirmation_and_deletes_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = LunePaths(support=tmp_path / "support", logs=tmp_path / "logs")
    monkeypatch.setattr(LunePaths, "defaults", classmethod(lambda _cls: paths))
    with MemoryStore(paths.database) as store:
        session_id = store.start_session("cli-session")
        turn_id = complete_turn(store, session_id, 1)
        for index in range(2):
            vector = np.zeros(EMBEDDING_DIMENSIONS, dtype=np.float32)
            vector[index] = 1.0
            assert store.add_memory(
                memory_id=f"memory-{index}",
                content=f"content-{index}",
                category="stable_preference",
                importance=0.5,
                embedding=vector,
                embedding_model=E5_MODEL_ID,
                embedding_revision=E5_MODEL_REVISION,
                source_turn_id=turn_id,
            )

    monkeypatch.setattr("builtins.input", lambda _prompt: "wrong-id")
    assert main(["memory", "forget", "memory-0"]) == 2
    monkeypatch.setattr("builtins.input", lambda _prompt: "memory-0")
    assert main(["memory", "forget", "memory-0"]) == 0

    with MemoryStore(paths.database) as reopened:
        assert [memory.id for memory in reopened.list_memories()] == ["memory-1"]
