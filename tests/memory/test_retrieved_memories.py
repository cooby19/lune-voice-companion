from __future__ import annotations

import numpy as np
import pytest

from lune.memory.store import EMBEDDING_DIMENSIONS, MemoryStore
from tests.memory.conftest import complete_turn

THREAD = "thread-retrieval"


def _embedding(index: int = 0) -> np.ndarray[tuple[int], np.dtype[np.float32]]:
    vector = np.zeros(EMBEDDING_DIMENSIONS, dtype=np.float32)
    vector[index] = 1.0
    return vector


def _add_memory(store: MemoryStore, turn_id: str, memory_id: str, *, index: int = 0) -> None:
    store.add_memory(
        memory_id=memory_id,
        content=f"她記得的第 {index} 件事",
        category="stable_preference",
        importance=0.6,
        embedding=_embedding(index),
        embedding_model="test-model",
        embedding_revision="test-revision",
        source_turn_id=turn_id,
    )


def _answered_turn(store: MemoryStore, generation_id: int) -> str:
    """Begin a turn that already holds both final messages but is still pending."""

    turn_id = store.begin_turn(THREAD, generation_id)
    store.accept_user_transcript(turn_id, f"user-{generation_id}")
    store.append_assistant_playback(turn_id, f"assistant-{generation_id}")
    return turn_id


def _assistant_memory_ids(store: MemoryStore) -> list[tuple[str, tuple[str, ...]]]:
    return [(message.role, message.memory_ids) for message in store.conversation_messages(THREAD)]


@pytest.fixture(autouse=True)
def thread(store: MemoryStore) -> None:
    store.start_session(THREAD)


def test_an_answered_turn_names_the_memories_retrieval_gave_it(store: MemoryStore) -> None:
    source = complete_turn(store, THREAD, 0)
    _add_memory(store, source, "memory-a", index=0)
    _add_memory(store, source, "memory-b", index=1)
    turn_id = _answered_turn(store, 1)

    assert store.record_retrieved_memories(turn_id, ["memory-b", "memory-a"]) == (
        "memory-b",
        "memory-a",
    )
    store.complete_turn(turn_id)

    # The ranking survives, so the interface can name the closest match first,
    # and the user's own words are never attributed to a memory.
    assert _assistant_memory_ids(store) == [
        ("user", ()),
        ("assistant", ()),
        ("user", ()),
        ("assistant", ("memory-b", "memory-a")),
    ]


def test_forgetting_a_memory_leaves_no_trace_on_the_message_that_used_it(
    store: MemoryStore,
) -> None:
    source = complete_turn(store, THREAD, 0)
    _add_memory(store, source, "memory-a", index=0)
    _add_memory(store, source, "memory-b", index=1)
    turn_id = _answered_turn(store, 1)
    store.record_retrieved_memories(turn_id, ["memory-a", "memory-b"])
    store.complete_turn(turn_id)

    assert store.forget_memory("memory-a") is True
    assert _assistant_memory_ids(store)[-1] == ("assistant", ("memory-b",))

    assert store.forget_memory("memory-b") is True
    assert _assistant_memory_ids(store)[-1] == ("assistant", ())


def test_a_memory_forgotten_before_the_turn_commits_is_skipped_not_raised(
    store: MemoryStore,
) -> None:
    source = complete_turn(store, THREAD, 0)
    _add_memory(store, source, "memory-a", index=0)
    turn_id = _answered_turn(store, 1)

    linked = store.record_retrieved_memories(turn_id, ["memory-a", "memory-gone"])

    assert linked == ("memory-a",)
    store.complete_turn(turn_id)
    assert _assistant_memory_ids(store)[-1] == ("assistant", ("memory-a",))


def test_a_cancelled_turn_records_nothing_a_reader_can_see(store: MemoryStore) -> None:
    source = complete_turn(store, THREAD, 0)
    _add_memory(store, source, "memory-a", index=0)
    turn_id = _answered_turn(store, 1)
    store.record_retrieved_memories(turn_id, ["memory-a"])

    store.cancel_turn(turn_id)

    assert [role for role, _ in _assistant_memory_ids(store)] == ["user", "assistant"]


def test_retrieval_can_only_be_recorded_while_the_turn_is_still_pending(
    store: MemoryStore,
) -> None:
    source = complete_turn(store, THREAD, 0)
    _add_memory(store, source, "memory-a", index=0)

    with pytest.raises(ValueError):
        store.record_retrieved_memories(source, ["memory-a"])


def test_repeated_identifiers_collapse_and_the_retriever_ceiling_is_enforced(
    store: MemoryStore,
) -> None:
    source = complete_turn(store, THREAD, 0)
    for index in range(6):
        _add_memory(store, source, f"memory-{index}", index=index)
    turn_id = _answered_turn(store, 1)

    assert store.record_retrieved_memories(turn_id, ["memory-0", "memory-0"]) == ("memory-0",)
    assert store.record_retrieved_memories(turn_id, []) == ()
    with pytest.raises(ValueError):
        store.record_retrieved_memories(turn_id, [f"memory-{index}" for index in range(6)])
