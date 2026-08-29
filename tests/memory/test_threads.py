from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from lune.memory.migrations import MIGRATIONS
from lune.memory.store import DEFAULT_CONVERSATION_TITLE, EMBEDDING_DIMENSIONS, MemoryStore


def _complete_turn(
    store: MemoryStore,
    thread_id: str,
    generation_id: int,
    *,
    at: datetime,
    user: str = "final user text",
    assistant: str = "final assistant text",
) -> str:
    turn_id = store.begin_turn(thread_id, generation_id, turn_id=f"turn-{generation_id}", at=at)
    store.accept_user_transcript(turn_id, user, at=at + timedelta(seconds=1))
    store.append_assistant_playback(turn_id, assistant, at=at + timedelta(seconds=2))
    store.complete_turn(turn_id, at=at + timedelta(seconds=3))
    return turn_id


def _embedding(index: int = 0) -> np.ndarray[tuple[int], np.dtype[np.float32]]:
    vector = np.zeros(EMBEDDING_DIMENSIONS, dtype=np.float32)
    vector[index] = 1.0
    return vector


def test_sessions_list_as_threads_with_private_titles_and_updated_timestamps(
    store: MemoryStore,
) -> None:
    start = datetime(2026, 8, 29, 1, tzinfo=UTC)
    first_id = store.start_session("thread-first", at=start)
    second_id = store.start_session(
        "thread-second", title="週末安排", at=start + timedelta(minutes=1)
    )

    initial = store.list_conversation_threads()

    assert [(thread.id, thread.title, thread.title_source) for thread in initial] == [
        (second_id, "週末安排", "manual"),
        (first_id, DEFAULT_CONVERSATION_TITLE, "default"),
    ]
    assert "週末安排" not in repr(initial[0])
    assert store.get_conversation_thread("missing-thread") is None
    with pytest.raises(ValueError, match="limit"):
        store.list_conversation_threads(limit=0)

    _complete_turn(store, first_id, 1, at=start + timedelta(minutes=2))
    after_turn = store.get_conversation_thread(first_id)

    assert after_turn is not None
    assert after_turn.updated_at == (start + timedelta(minutes=2, seconds=3)).isoformat()
    assert [thread.id for thread in store.list_conversation_threads(limit=1)] == [first_id]

    renamed = store.rename_conversation_thread(
        first_id, "晨間整理", at=start + timedelta(minutes=3)
    )

    assert (renamed.title, renamed.title_source, renamed.updated_at) == (
        "晨間整理",
        "manual",
        (start + timedelta(minutes=3)).isoformat(),
    )
    with pytest.raises(ValueError, match="printable"):
        store.rename_conversation_thread(first_id, "unsafe\ntitle")
    with pytest.raises(ValueError, match="unknown"):
        store.rename_conversation_thread("missing-thread", "任何標題")

    store.end_session(second_id, at=start + timedelta(minutes=4))
    closed = store.get_conversation_thread(second_id)
    assert closed is not None
    assert (closed.ended_at, closed.updated_at) == (
        (start + timedelta(minutes=4)).isoformat(),
        (start + timedelta(minutes=4)).isoformat(),
    )


def test_generated_title_is_one_time_and_never_overwrites_a_manual_title(
    store: MemoryStore,
) -> None:
    start = datetime(2026, 8, 29, 2, tzinfo=UTC)
    thread_id = store.start_session("thread-title", at=start)

    assert not store.set_generated_conversation_title(
        thread_id, "太早產生", at=start + timedelta(seconds=1)
    )
    _complete_turn(store, thread_id, 1, at=start + timedelta(minutes=1))
    assert store.set_generated_conversation_title(
        thread_id, "一起規劃旅行", at=start + timedelta(minutes=2)
    )
    assert not store.set_generated_conversation_title(
        thread_id, "不應覆寫", at=start + timedelta(minutes=3)
    )

    generated = store.get_conversation_thread(thread_id)
    assert generated is not None
    assert (generated.title, generated.title_source) == ("一起規劃旅行", "generated")

    store.rename_conversation_thread(thread_id, "我的手動標題", at=start + timedelta(minutes=4))

    assert not store.set_generated_conversation_title(
        thread_id, "仍不應覆寫", at=start + timedelta(minutes=5)
    )
    renamed = store.get_conversation_thread(thread_id)
    assert renamed is not None
    assert (renamed.title, renamed.title_source) == ("我的手動標題", "manual")


def test_conversation_messages_only_include_completed_turns_and_text_deliveries(
    store: MemoryStore,
) -> None:
    start = datetime(2026, 8, 29, 3, tzinfo=UTC)
    thread_id = store.start_session("thread-messages", at=start)
    completed = store.begin_turn(thread_id, 1, turn_id="turn-completed", at=start)
    store.accept_user_transcript(completed, "保留的使用者訊息", at=start + timedelta(seconds=1))
    message_id = store.append_assistant_text_delivery(
        completed, "第一段", at=start + timedelta(seconds=2)
    )
    assert (
        store.append_assistant_text_delivery(completed, "第二段", at=start + timedelta(seconds=3))
        == message_id
    )
    store.complete_turn(completed, at=start + timedelta(seconds=4))

    cancelled = store.begin_turn(thread_id, 2, turn_id="turn-cancelled", at=start)
    store.accept_user_transcript(cancelled, "不得顯示的取消內容", at=start)
    store.append_assistant_playback(cancelled, "不得顯示的回覆", at=start)
    store.cancel_turn(cancelled, at=start)
    pending = store.begin_turn(thread_id, 3, turn_id="turn-pending", at=start)
    store.accept_user_transcript(pending, "不得顯示的待處理內容", at=start)

    messages = store.conversation_messages(thread_id)

    assert [(message.turn_id, message.role, message.content) for message in messages] == [
        (completed, "user", "保留的使用者訊息"),
        (completed, "assistant", "第一段第二段"),
    ]
    assert messages[1].id == message_id
    assert messages[1].created_at == (start + timedelta(seconds=2)).isoformat()
    assert all("不得顯示" not in repr(message) for message in messages)
    assert store.conversation_messages("missing-thread") == ()
    with pytest.raises(ValueError, match="no longer pending"):
        store.append_assistant_text_delivery(completed, "不應寫入")


def test_memory_sources_are_presentable_and_forget_removes_only_one_memory(
    store: MemoryStore,
) -> None:
    start = datetime(2026, 8, 29, 4, tzinfo=UTC)
    thread_id = store.start_session("thread-memory", at=start)
    turn_id = _complete_turn(store, thread_id, 1, at=start)
    requested = store.add_memory(
        memory_id="memory-requested",
        content="使用者明確交代的事項",
        category="explicit_request",
        importance=0.8,
        embedding=_embedding(0),
        embedding_model="test-model",
        embedding_revision="test-revision",
        source_turn_id=turn_id,
        at=start + timedelta(minutes=1),
    )
    observed = store.add_memory(
        memory_id="memory-observed",
        content="Lune 注意到的偏好",
        category="stable_preference",
        importance=0.6,
        embedding=_embedding(1),
        embedding_model="test-model",
        embedding_revision="test-revision",
        source_turn_id=turn_id,
        at=start + timedelta(minutes=2),
    )
    delegated = store.add_memory(
        memory_id="memory-delegated",
        content="由使用者委託的另一件事",
        category="explicit_plan",
        importance=0.7,
        embedding=_embedding(2),
        embedding_model="test-model",
        embedding_revision="test-revision",
        source_turn_id=turn_id,
        source="user_requested",
        at=start + timedelta(minutes=3),
    )

    assert requested is not None and requested.source == "user_requested"
    assert observed is not None and observed.source == "lune_observed"
    assert delegated is not None and delegated.source == "user_requested"
    assert [(memory.id, memory.source) for memory in store.list_memories()] == [
        ("memory-requested", "user_requested"),
        ("memory-observed", "lune_observed"),
        ("memory-delegated", "user_requested"),
    ]
    assert store.forget_memory("memory-observed")
    assert not store.forget_memory("missing-memory")
    assert [memory.id for memory in store.list_memories()] == [
        "memory-requested",
        "memory-delegated",
    ]

    with pytest.raises(ValueError, match="source"):
        store.add_memory(
            memory_id="memory-invalid-source",
            content="不應寫入",
            category="stable_preference",
            importance=0.5,
            embedding=_embedding(3),
            embedding_model="test-model",
            embedding_revision="test-revision",
            source_turn_id=turn_id,
            source="not-a-source",  # type: ignore[arg-type]
        )


def test_v1_database_migrates_existing_sessions_messages_and_memory_sources(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    started_at = datetime(2026, 8, 29, 5, tzinfo=UTC)
    ended_at = started_at + timedelta(minutes=4)
    connection = sqlite3.connect(path)
    connection.executescript(MIGRATIONS[0].sql)
    connection.execute("PRAGMA user_version = 1")
    connection.execute(
        "INSERT INTO sessions (id, started_at, ended_at) VALUES (?, ?, ?)",
        ("legacy-thread", started_at.isoformat(), ended_at.isoformat()),
    )
    connection.execute(
        """
        INSERT INTO turns
            (id, session_id, generation_id, sequence, status, started_at, completed_at)
        VALUES (?, ?, ?, ?, 'complete', ?, ?)
        """,
        ("legacy-turn", "legacy-thread", 1, 1, started_at.isoformat(), ended_at.isoformat()),
    )
    connection.execute(
        """
        INSERT INTO messages (id, turn_id, role, content, created_at)
        VALUES (?, ?, 'user', ?, ?)
        """,
        ("legacy-message", "legacy-turn", "legacy user", started_at.isoformat()),
    )
    vector = _embedding().tobytes()
    for memory_id, content, category in (
        ("legacy-request", "legacy request", "explicit_request"),
        ("legacy-observed", "legacy observed", "stable_preference"),
    ):
        connection.execute(
            """
            INSERT INTO long_term_memories
                (id, content, normalized_content, category, importance, embedding,
                 embedding_dimensions, embedding_model, embedding_revision, embedding_dtype,
                 source_turn_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'float32', ?, ?)
            """,
            (
                memory_id,
                content,
                content,
                category,
                0.5,
                vector,
                EMBEDDING_DIMENSIONS,
                "legacy-model",
                "legacy-revision",
                "legacy-turn",
                started_at.isoformat(),
            ),
        )
    connection.commit()
    connection.close()

    with MemoryStore(path) as store:
        assert store.schema_version == 2
        thread = store.get_conversation_thread("legacy-thread")
        assert thread is not None
        assert (thread.title, thread.title_source, thread.updated_at, thread.ended_at) == (
            DEFAULT_CONVERSATION_TITLE,
            "default",
            ended_at.isoformat(),
            ended_at.isoformat(),
        )
        legacy_messages = store.conversation_messages("legacy-thread")
        assert [(message.id, message.created_at) for message in legacy_messages] == [
            ("legacy-message", started_at.isoformat())
        ]
        assert {memory.id: memory.source for memory in store.list_memories()} == {
            "legacy-request": "user_requested",
            "legacy-observed": "lune_observed",
        }
