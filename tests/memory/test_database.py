from __future__ import annotations

import sqlite3
import stat
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from lune.llm.contracts import AttemptUsageFrame
from lune.memory.store import MemoryStore
from lune.memory.usage import persistent_budget_ledger
from tests.memory.conftest import complete_turn

NOW = datetime(2026, 8, 27, 2, tzinfo=UTC)


def test_migrations_are_idempotent_and_connections_are_private(tmp_path: Path) -> None:
    path = tmp_path / "private" / "lune.sqlite3"
    with MemoryStore(path) as first:
        assert first.schema_version == 3
        assert first.pragma("foreign_keys") == 1
        assert first.pragma("journal_mode") == "wal"
        assert first.pragma("busy_timeout") == 5_000
        assert first.pragma("secure_delete") == 1

    with MemoryStore(path) as reopened:
        assert reopened.schema_version == 3

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    connection = sqlite3.connect(path)
    tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    connection.close()
    assert {
        "sessions",
        "turns",
        "messages",
        "summaries",
        "long_term_memories",
        "relationship_state",
        "relationship_events",
        "llm_usage",
        "turn_retrieved_memories",
    } <= tables


def test_ephemeral_store_never_creates_a_database_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with MemoryStore.ephemeral() as store:
        session_id = store.start_session("ephemeral-session")
        complete_turn(store, session_id, 1, user="live speech", assistant="heard response")
        assert store.pragma("journal_mode") == "memory"
        assert len(store.recent_complete_turns(session_id)) == 1

    assert not (tmp_path / ":memory:").exists()


def test_only_final_user_and_played_assistant_text_are_committed(store: MemoryStore) -> None:
    session_id = store.start_session("session-1")
    cancelled = store.begin_turn(session_id, 1)
    store.accept_user_transcript(cancelled, "final user text")
    store.append_assistant_playback(cancelled, "heard fragment")
    store.cancel_turn(cancelled)
    incomplete = store.begin_turn(session_id, 2)
    store.accept_user_transcript(incomplete, "not a complete turn")

    assert store.recent_complete_turns(session_id) == ()

    completed = complete_turn(store, session_id, 3, user="accepted", assistant="played")
    turns = store.recent_complete_turns(session_id)
    assert [turn.id for turn in turns] == [completed]
    assert [message.content for message in turns[0].messages] == ["accepted", "played"]
    assert "accepted" not in repr(turns[0])


def test_usage_survives_restart_and_restores_confirmed_budget(tmp_path: Path) -> None:
    path = tmp_path / "private" / "lune.sqlite3"
    with MemoryStore(path) as store:
        ledger = persistent_budget_ledger(store)
        reservation = ledger.reserve_model(
            at=NOW,
            model="gpt-5.6-terra",
            max_input_tokens=8_000,
            max_output_tokens=192,
        )
        settled = ledger.settle(
            reservation.attempt_id,
            AttemptUsageFrame(
                generation_id=4,
                attempt_id=reservation.attempt_id,
                input_tokens=1_000,
                output_tokens=100,
            ),
        )
        assert store.confirmed_usage_totals() == {"2026-08": settled.charged_twd}

    with MemoryStore(path) as reopened:
        restored = persistent_budget_ledger(reopened)
        assert restored.total_with_reservations(NOW) == settled.charged_twd
        assert restored.total_with_reservations(NOW) > Decimal()
