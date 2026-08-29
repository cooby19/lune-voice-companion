from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from lune.memory.store import EMBEDDING_DIMENSIONS, MemoryStore, StoreChange

START = datetime(2026, 8, 29, 6, tzinfo=UTC)


def _embedding(index: int = 0) -> np.ndarray[tuple[int], np.dtype[np.float32]]:
    vector = np.zeros(EMBEDDING_DIMENSIONS, dtype=np.float32)
    vector[index] = 1.0
    return vector


def _add_memory(store: MemoryStore, turn_id: str, memory_id: str, *, index: int = 0) -> object:
    return store.add_memory(
        memory_id=memory_id,
        content=f"記得的第 {index} 件事",
        category="stable_preference",
        importance=0.6,
        embedding=_embedding(index),
        embedding_model="test-model",
        embedding_revision="test-revision",
        source_turn_id=turn_id,
        at=START + timedelta(minutes=index + 1),
    )


def _recorder(store: MemoryStore) -> list[StoreChange]:
    changes: list[StoreChange] = []
    store.set_change_listener(changes.append)
    return changes


def test_starting_and_renaming_a_thread_each_announce_the_thread(store: MemoryStore) -> None:
    changes = _recorder(store)

    thread_id = store.start_session("thread-one", at=START)
    store.rename_conversation_thread(thread_id, "改過的標題", at=START + timedelta(minutes=1))

    assert changes == [
        StoreChange("thread", thread_id="thread-one"),
        StoreChange("thread", thread_id="thread-one"),
    ]


def test_a_generated_title_announces_only_when_it_actually_replaced_one(
    store: MemoryStore,
) -> None:
    thread_id = store.start_session("thread-titled", at=START)
    turn_id = store.begin_turn(thread_id, 1, at=START)
    store.accept_user_transcript(turn_id, "使用者說的話", at=START)
    store.append_assistant_playback(turn_id, "Lune 回的話", at=START)
    store.complete_turn(turn_id, at=START)
    changes = _recorder(store)

    assert store.set_generated_conversation_title(thread_id, "自動標題", at=START) is True
    # A manual title outranks the automatic one, so the second call changes
    # nothing and must not claim otherwise.
    store.rename_conversation_thread(thread_id, "手動標題", at=START)
    changes.clear()
    assert store.set_generated_conversation_title(thread_id, "再一次", at=START) is False

    assert changes == []


def test_completing_a_turn_announces_its_messages_and_the_thread(store: MemoryStore) -> None:
    thread_id = store.start_session("thread-turn", at=START)
    turn_id = store.begin_turn(thread_id, 1, turn_id="turn-one", at=START)
    changes = _recorder(store)

    # Interim writes stay silent: `conversation_messages` hides a pending turn,
    # so announcing them would offer the UI text it cannot read back.
    store.accept_user_transcript(turn_id, "使用者說的話", at=START)
    store.append_assistant_playback(turn_id, "Lune 說的第一句。", at=START)
    store.append_assistant_playback(turn_id, "還有第二句。", at=START)
    assert changes == []

    store.complete_turn(turn_id, at=START + timedelta(seconds=5))

    assert changes == [
        StoreChange("messages", thread_id="thread-turn", turn_id="turn-one"),
        StoreChange("thread", thread_id="thread-turn"),
    ]


def test_a_cancelled_turn_announces_nothing(store: MemoryStore) -> None:
    thread_id = store.start_session("thread-cancelled", at=START)
    turn_id = store.begin_turn(thread_id, 1, at=START)
    store.accept_user_transcript(turn_id, "被打斷的話", at=START)
    changes = _recorder(store)

    store.cancel_turn(turn_id, at=START)

    assert changes == []


def test_adding_and_forgetting_a_memory_announce_the_memory_list(store: MemoryStore) -> None:
    thread_id = store.start_session("thread-memory", at=START)
    turn_id = store.begin_turn(thread_id, 1, at=START)
    store.accept_user_transcript(turn_id, "使用者說的話", at=START)
    store.append_assistant_playback(turn_id, "Lune 回的話", at=START)
    store.complete_turn(turn_id, at=START)
    changes = _recorder(store)

    assert _add_memory(store, turn_id, "memory-one") is not None
    # The same content again is deduplicated rather than stored, so there is
    # nothing new for the memory view to show.
    assert _add_memory(store, turn_id, "memory-two") is None
    assert store.forget_memory("memory-one") is True
    assert store.forget_memory("memory-one") is False

    assert changes == [StoreChange("memories"), StoreChange("memories")]


def test_a_failing_listener_cannot_break_a_committed_write(store: MemoryStore) -> None:
    def explode(_change: StoreChange) -> None:
        raise RuntimeError("the observer is broken")

    store.set_change_listener(explode)

    thread_id = store.start_session("thread-safe", at=START)

    assert store.get_conversation_thread(thread_id) is not None


def test_clearing_the_listener_stops_the_announcements(store: MemoryStore) -> None:
    changes = _recorder(store)
    store.start_session("thread-heard", at=START)
    store.set_change_listener(None)

    store.start_session("thread-unheard", at=START)

    assert changes == [StoreChange("thread", thread_id="thread-heard")]


def test_a_listener_may_read_the_committed_rows_back(store: MemoryStore) -> None:
    """The notice carries identifiers only, so reading back must be safe.

    It fires after the write lock is released, which is what lets an observer
    query the store from the same task without deadlocking on it.
    """

    titles: list[str] = []

    def read_back(change: StoreChange) -> None:
        assert change.thread_id is not None
        thread = store.get_conversation_thread(change.thread_id)
        assert thread is not None
        titles.append(thread.title)

    store.set_change_listener(read_back)
    thread_id = store.start_session("thread-readable", title="原始標題", at=START)
    store.rename_conversation_thread(thread_id, "新標題", at=START)

    assert titles == ["原始標題", "新標題"]


def test_a_change_carries_identifiers_and_nothing_else() -> None:
    """A queued or dropped notice must never be somewhere private text lives."""

    assert StoreChange.__slots__ == ("kind", "thread_id", "turn_id")
