from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest

from lune.memory.embedding import E5_MODEL_ID, E5_MODEL_REVISION, E5MemoryRetriever, E5SetupRequired
from lune.memory.store import MemoryStore
from lune.pipeline.enricher import ContextEnricher
from tests.memory.conftest import complete_turn
from tests.pipeline.harness import HashEncoder

type FloatArray = npt.NDArray[np.float32]

SESSION = "session-enricher"


class BrokenEncoder:
    model_id = E5_MODEL_ID
    revision = E5_MODEL_REVISION

    def encode_query(self, query: str) -> FloatArray:
        raise E5SetupRequired("model_load_failed")

    def encode_passages(self, passages: Sequence[str]) -> FloatArray:
        raise E5SetupRequired("model_load_failed")


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    instance = MemoryStore(tmp_path / "private" / "lune.sqlite3")
    instance.start_session(SESSION)
    yield instance
    instance.close()


def test_the_live_user_message_reaches_the_model_before_the_turn_completes(
    store: MemoryStore,
) -> None:
    enricher = ContextEnricher(store, E5MemoryRetriever(store, HashEncoder()))
    context = enricher.enrich(SESSION, user_text="現在幾點")

    assert [message.role for message in context.recent_messages] == ["user"]
    assert context.recent_messages[-1].content == "現在幾點"
    assert context.summary is None
    assert context.relevant_memories == ()


def test_history_is_capped_at_twelve_completed_turns(store: MemoryStore) -> None:
    for index in range(15):
        complete_turn(store, SESSION, index)
    enricher = ContextEnricher(store, E5MemoryRetriever(store, HashEncoder()))

    context = enricher.enrich(SESSION, user_text="最新的問題")

    assert len(context.recent_messages) == 12 * 2 + 1
    assert context.recent_messages[0].content == "user-3"
    assert context.recent_messages[-1].content == "最新的問題"


def test_a_rolling_summary_is_carried_as_developer_context(store: MemoryStore) -> None:
    turns = [complete_turn(store, SESSION, index) for index in range(4)]
    stored = store.unsummarized_complete_turns(SESSION)
    store.advance_summary(SESSION, stored[: len(turns)], "先前對話摘要")
    enricher = ContextEnricher(store, E5MemoryRetriever(store, HashEncoder()))

    context = enricher.enrich(SESSION, user_text="接著呢")

    assert context.summary == "先前對話摘要"
    assert len(context.recent_messages) == 1


def test_retrieved_memories_stay_inside_the_cloud_context_budget(store: MemoryStore) -> None:
    turn_id = complete_turn(store, SESSION, 0)
    encoder = HashEncoder()
    retriever = E5MemoryRetriever(store, encoder)
    for index in range(8):
        content = f"記憶-{index}"
        store.add_memory(
            memory_id=f"memory-{index}",
            content=content,
            category="stable_preference",
            importance=0.5,
            embedding=retriever.embed_passage(content),
            embedding_model=encoder.model_id,
            embedding_revision=encoder.revision,
            source_turn_id=turn_id,
        )
    enricher = ContextEnricher(store, retriever)

    context = enricher.enrich(SESSION, user_text="記憶-3")

    assert len(context.relevant_memories) <= 5
    assert sum(len(memory) for memory in context.relevant_memories) <= 1_200


def test_a_missing_local_encoder_degrades_the_answer_instead_of_failing_the_turn(
    store: MemoryStore,
) -> None:
    enricher = ContextEnricher(store, E5MemoryRetriever(store, BrokenEncoder()))

    context = enricher.enrich(SESSION, user_text="還記得嗎")

    assert context.relevant_memories == ()
    assert enricher.retrieval_available is False
    assert enricher.enrich(SESSION, user_text="再問一次").relevant_memories == ()


def test_configured_bounds_are_validated(store: MemoryStore) -> None:
    with pytest.raises(ValueError):
        ContextEnricher(store, None, max_memories=6)
    with pytest.raises(ValueError):
        ContextEnricher(store, None, max_recent_turns=13)
    with pytest.raises(ValueError):
        ContextEnricher(store, None, max_memory_characters=1_201)
