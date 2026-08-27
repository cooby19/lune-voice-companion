"""Private SQLite store with commit boundaries matching heard conversation state."""

from __future__ import annotations

import os
import sqlite3
import stat
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import numpy as np
import numpy.typing as npt

from lune.llm.budget import SettledAttempt
from lune.llm.prompt import ConversationMessage, PromptContext
from lune.memory.migrations import MIGRATIONS

EMBEDDING_DIMENSIONS = 384
type FloatArray = npt.NDArray[np.float32]
_MEMORY_CATEGORIES = frozenset(
    {"stable_preference", "important_person_or_event", "explicit_plan", "explicit_request"}
)


@dataclass(frozen=True, slots=True)
class StoredTurn:
    id: str
    session_id: str
    generation_id: int
    sequence: int
    messages: tuple[ConversationMessage, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class SummaryCoverage:
    id: str
    session_id: str
    start_turn_sequence: int
    end_turn_sequence: int
    covered_turn_count: int
    content: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class StoredMemory:
    id: str
    category: str
    importance: float
    source_turn_id: str
    created_at: str
    content: str = field(repr=False)
    embedding: FloatArray = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class RelationshipEvent:
    id: str
    session_id: str
    turn_id: str
    generation_id: int
    delta: int
    reason: str = field(repr=False)
    resulting_affinity: int
    created_at: str


class MemoryStore:
    """Own one configured SQLite connection and serialize all local mutations."""

    def __init__(self, database_path: Path, *, busy_timeout_ms: int = 5_000) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy timeout must be positive")
        self._path = database_path
        self._lock = threading.RLock()
        self._closed = False
        self._prepare_private_path()
        self._connection = sqlite3.connect(
            database_path,
            timeout=busy_timeout_ms / 1_000,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._configure_connection(busy_timeout_ms)
        self._apply_migrations()
        self._enforce_sidecar_permissions()

    @property
    def database_path(self) -> Path:
        return self._path

    @property
    def schema_version(self) -> int:
        row = self._execute("PRAGMA user_version").fetchone()
        assert row is not None
        return int(row[0])

    def pragma(self, name: str) -> object:
        if name not in {"foreign_keys", "journal_mode", "busy_timeout", "secure_delete"}:
            raise ValueError("unsupported pragma")
        row = self._execute(f"PRAGMA {name}").fetchone()
        assert row is not None
        return row[0]

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def start_session(self, session_id: str | None = None, *, at: datetime | None = None) -> str:
        identifier = _validated_id(session_id or uuid4().hex, "session")
        with self._write():
            self._connection.execute(
                "INSERT INTO sessions (id, started_at) VALUES (?, ?)",
                (identifier, _timestamp(at)),
            )
        return identifier

    def end_session(self, session_id: str, *, at: datetime | None = None) -> None:
        with self._write():
            cursor = self._connection.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                (_timestamp(at), _validated_id(session_id, "session")),
            )
            if cursor.rowcount != 1:
                raise ValueError("unknown or already ended session")

    def begin_turn(
        self,
        session_id: str,
        generation_id: int,
        *,
        turn_id: str | None = None,
        at: datetime | None = None,
    ) -> str:
        if generation_id < 0:
            raise ValueError("generation ID cannot be negative")
        identifier = _validated_id(turn_id or uuid4().hex, "turn")
        session_id = _validated_id(session_id, "session")
        with self._write():
            row = self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            assert row is not None
            try:
                self._connection.execute(
                    """
                    INSERT INTO turns
                        (id, session_id, generation_id, sequence, status, started_at)
                    VALUES (?, ?, ?, ?, 'pending', ?)
                    """,
                    (identifier, session_id, generation_id, int(row[0]), _timestamp(at)),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("unknown session or duplicate turn") from error
        return identifier

    def accept_user_transcript(
        self, turn_id: str, content: str, *, at: datetime | None = None
    ) -> str:
        """Persist only a final transcript; callers never pass interim text here."""

        return self._insert_message(turn_id, "user", content, at=at)

    def append_assistant_playback(
        self, turn_id: str, content: str, *, at: datetime | None = None
    ) -> str:
        """Append only text whose corresponding audio was confirmed as played."""

        clean = _validated_content(content, "assistant playback")
        turn_id = _validated_id(turn_id, "turn")
        with self._write():
            self._require_pending_turn(turn_id)
            row = self._connection.execute(
                "SELECT id, content FROM messages WHERE turn_id = ? AND role = 'assistant'",
                (turn_id,),
            ).fetchone()
            if row is None:
                message_id = uuid4().hex
                self._connection.execute(
                    """
                    INSERT INTO messages (id, turn_id, role, content, created_at)
                    VALUES (?, ?, 'assistant', ?, ?)
                    """,
                    (message_id, turn_id, clean, _timestamp(at)),
                )
                return message_id
            self._connection.execute(
                "UPDATE messages SET content = ? WHERE id = ?",
                (str(row["content"]) + clean, str(row["id"])),
            )
            return str(row["id"])

    def complete_turn(self, turn_id: str, *, at: datetime | None = None) -> None:
        turn_id = _validated_id(turn_id, "turn")
        with self._write():
            self._require_pending_turn(turn_id)
            roles = {
                str(row[0])
                for row in self._connection.execute(
                    "SELECT role FROM messages WHERE turn_id = ?", (turn_id,)
                )
            }
            if roles != {"user", "assistant"}:
                raise ValueError("a complete turn requires final user and played assistant text")
            self._connection.execute(
                "UPDATE turns SET status = 'complete', completed_at = ? WHERE id = ?",
                (_timestamp(at), turn_id),
            )

    def cancel_turn(self, turn_id: str, *, at: datetime | None = None) -> None:
        turn_id = _validated_id(turn_id, "turn")
        with self._write():
            self._require_pending_turn(turn_id)
            self._connection.execute(
                "UPDATE turns SET status = 'cancelled', completed_at = ? WHERE id = ?",
                (_timestamp(at), turn_id),
            )

    def recent_complete_turns(self, session_id: str, *, limit: int = 12) -> tuple[StoredTurn, ...]:
        if limit <= 0:
            raise ValueError("turn limit must be positive")
        rows = self._execute(
            """
            SELECT id, session_id, generation_id, sequence
            FROM turns
            WHERE session_id = ? AND status = 'complete'
            ORDER BY sequence DESC
            LIMIT ?
            """,
            (_validated_id(session_id, "session"), limit),
        ).fetchall()
        return tuple(self._stored_turn(row) for row in reversed(rows))

    def unsummarized_complete_turns(self, session_id: str) -> tuple[StoredTurn, ...]:
        session_id = _validated_id(session_id, "session")
        coverage = self.get_summary(session_id)
        after = coverage.end_turn_sequence if coverage is not None else 0
        rows = self._execute(
            """
            SELECT id, session_id, generation_id, sequence
            FROM turns
            WHERE session_id = ? AND status = 'complete' AND sequence > ?
            ORDER BY sequence
            """,
            (session_id, after),
        ).fetchall()
        return tuple(self._stored_turn(row) for row in rows)

    def turn_matches(self, turn_id: str, session_id: str, generation_id: int) -> bool:
        if generation_id < 0:
            return False
        row = self._execute(
            "SELECT session_id, generation_id, status FROM turns WHERE id = ?",
            (_validated_id(turn_id, "turn"),),
        ).fetchone()
        return bool(
            row is not None
            and str(row["session_id"]) == _validated_id(session_id, "session")
            and int(row["generation_id"]) == generation_id
            and str(row["status"]) == "pending"
        )

    def get_summary(self, session_id: str) -> SummaryCoverage | None:
        row = self._execute(
            """
            SELECT id, session_id, start_turn_sequence, end_turn_sequence,
                   covered_turn_count, content
            FROM summaries WHERE session_id = ?
            """,
            (_validated_id(session_id, "session"),),
        ).fetchone()
        if row is None:
            return None
        return SummaryCoverage(
            id=str(row["id"]),
            session_id=str(row["session_id"]),
            start_turn_sequence=int(row["start_turn_sequence"]),
            end_turn_sequence=int(row["end_turn_sequence"]),
            covered_turn_count=int(row["covered_turn_count"]),
            content=str(row["content"]),
        )

    def advance_summary(
        self,
        session_id: str,
        turns: Sequence[StoredTurn],
        content: str,
        *,
        at: datetime | None = None,
    ) -> SummaryCoverage:
        if len(turns) != 4:
            raise ValueError("rolling summary must advance by exactly four complete turns")
        clean = _validated_content(content, "summary")
        session_id = _validated_id(session_id, "session")
        if any(turn.session_id != session_id for turn in turns):
            raise ValueError("summary turns belong to a different session")
        sequences = [turn.sequence for turn in turns]
        if sequences != sorted(set(sequences)):
            raise ValueError("summary turns must be unique and ordered")
        with self._write():
            current_row = self._connection.execute(
                """
                SELECT id, start_turn_sequence, end_turn_sequence, covered_turn_count
                FROM summaries WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            previous_end = int(current_row["end_turn_sequence"]) if current_row else 0
            expected_rows = self._connection.execute(
                """
                SELECT id, sequence FROM turns
                WHERE session_id = ? AND status = 'complete' AND sequence > ?
                ORDER BY sequence LIMIT 4
                """,
                (session_id, previous_end),
            ).fetchall()
            if [(str(row["id"]), int(row["sequence"])) for row in expected_rows] != [
                (turn.id, turn.sequence) for turn in turns
            ]:
                raise ValueError("summary coverage must be continuous and non-overlapping")
            now = _timestamp(at)
            if current_row is None:
                summary_id = uuid4().hex
                start = sequences[0]
                count = 4
                self._connection.execute(
                    """
                    INSERT INTO summaries
                        (id, session_id, start_turn_sequence, end_turn_sequence,
                         covered_turn_count, content, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (summary_id, session_id, start, sequences[-1], count, clean, now),
                )
            else:
                summary_id = str(current_row["id"])
                start = int(current_row["start_turn_sequence"])
                count = int(current_row["covered_turn_count"]) + 4
                self._connection.execute(
                    """
                    UPDATE summaries
                    SET end_turn_sequence = ?, covered_turn_count = ?, content = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (sequences[-1], count, clean, now, summary_id),
                )
        return SummaryCoverage(summary_id, session_id, start, sequences[-1], count, clean)

    def build_prompt_context(
        self, session_id: str, *, relevant_memories: Sequence[str] = ()
    ) -> PromptContext:
        turns = self.unsummarized_complete_turns(session_id)[-12:]
        messages = tuple(message for turn in turns for message in turn.messages)
        if not messages:
            raise ValueError("at least one complete turn is required")
        summary = self.get_summary(session_id)
        return PromptContext(
            recent_messages=messages,
            summary=summary.content if summary is not None else None,
            relevant_memories=tuple(relevant_memories),
        )

    def add_memory(
        self,
        *,
        memory_id: str,
        content: str,
        category: str,
        importance: float,
        embedding: FloatArray,
        embedding_model: str,
        embedding_revision: str,
        source_turn_id: str,
        at: datetime | None = None,
    ) -> StoredMemory | None:
        memory_id = _validated_id(memory_id, "memory")
        clean = _validated_content(content, "memory")
        normalized = _normalize_content(clean)
        if category not in _MEMORY_CATEGORIES:
            raise ValueError("unsupported memory category")
        if not 0.0 <= importance <= 1.0:
            raise ValueError("memory importance must be between zero and one")
        vector = _normalized_embedding(embedding)
        created_at = _timestamp(at)
        try:
            with self._write():
                self._connection.execute(
                    """
                    INSERT INTO long_term_memories
                        (id, content, normalized_content, category, importance, embedding,
                         embedding_dimensions, embedding_model, embedding_revision,
                         embedding_dtype, source_turn_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'float32', ?, ?)
                    """,
                    (
                        memory_id,
                        clean,
                        normalized,
                        category,
                        importance,
                        vector.tobytes(),
                        EMBEDDING_DIMENSIONS,
                        embedding_model,
                        embedding_revision,
                        _validated_id(source_turn_id, "turn"),
                        created_at,
                    ),
                )
        except sqlite3.IntegrityError as error:
            if "normalized_content" in str(error) or "long_term_memories.id" in str(error):
                return None
            raise ValueError("memory source turn does not exist") from error
        return StoredMemory(
            memory_id,
            category,
            importance,
            source_turn_id,
            created_at,
            clean,
            vector,
        )

    def list_memories(self) -> tuple[StoredMemory, ...]:
        rows = self._execute(
            """
            SELECT id, content, category, importance, embedding, source_turn_id, created_at
            FROM long_term_memories ORDER BY created_at, id
            """
        ).fetchall()
        return tuple(_memory_from_row(row) for row in rows)

    def forget_memory(self, exact_id: str) -> bool:
        with self._write():
            cursor = self._connection.execute(
                "DELETE FROM long_term_memories WHERE id = ?",
                (_validated_id(exact_id, "memory"),),
            )
            return cursor.rowcount == 1

    def affinity(self) -> int:
        row = self._execute("SELECT affinity FROM relationship_state WHERE id = 1").fetchone()
        assert row is not None
        return int(row[0])

    def apply_affinity(
        self,
        *,
        event_id: str,
        session_id: str,
        turn_id: str,
        generation_id: int,
        delta: int,
        reason: str,
        at: datetime | None = None,
    ) -> RelationshipEvent | None:
        if delta not in {-1, 1}:
            raise ValueError("affinity delta must be exactly -1 or 1")
        if generation_id < 0:
            raise ValueError("generation ID cannot be negative")
        event_id = _validated_id(event_id, "relationship event")
        session_id = _validated_id(session_id, "session")
        turn_id = _validated_id(turn_id, "turn")
        reason = _validated_content(reason, "affinity reason")
        created_at = _timestamp(at)
        with self._write():
            duplicate = self._connection.execute(
                "SELECT 1 FROM relationship_events WHERE id = ? OR turn_id = ?",
                (event_id, turn_id),
            ).fetchone()
            if duplicate is not None:
                return None
            turn = self._connection.execute(
                "SELECT session_id, generation_id FROM turns WHERE id = ?",
                (turn_id,),
            ).fetchone()
            if turn is None or str(turn["session_id"]) != session_id:
                raise ValueError("affinity turn does not belong to the session")
            if int(turn["generation_id"]) != generation_id:
                raise ValueError("affinity generation does not match the turn")
            session_delta_row = self._connection.execute(
                "SELECT COALESCE(SUM(delta), 0) FROM relationship_events WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            assert session_delta_row is not None
            if abs(int(session_delta_row[0]) + delta) > 3:
                return None
            current_row = self._connection.execute(
                "SELECT affinity FROM relationship_state WHERE id = 1"
            ).fetchone()
            assert current_row is not None
            resulting = int(current_row[0]) + delta
            if not 0 <= resulting <= 100:
                return None
            self._connection.execute(
                "UPDATE relationship_state SET affinity = ?, updated_at = ? WHERE id = 1",
                (resulting, created_at),
            )
            self._connection.execute(
                """
                INSERT INTO relationship_events
                    (id, session_id, turn_id, generation_id, delta, reason,
                     resulting_affinity, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    session_id,
                    turn_id,
                    generation_id,
                    delta,
                    reason,
                    resulting,
                    created_at,
                ),
            )
        return RelationshipEvent(
            event_id,
            session_id,
            turn_id,
            generation_id,
            delta,
            reason,
            resulting,
            created_at,
        )

    def relationship_events(self, session_id: str) -> tuple[RelationshipEvent, ...]:
        rows = self._execute(
            """
            SELECT id, session_id, turn_id, generation_id, delta, reason,
                   resulting_affinity, created_at
            FROM relationship_events WHERE session_id = ? ORDER BY created_at, id
            """,
            (_validated_id(session_id, "session"),),
        ).fetchall()
        return tuple(
            RelationshipEvent(
                str(row["id"]),
                str(row["session_id"]),
                str(row["turn_id"]),
                int(row["generation_id"]),
                int(row["delta"]),
                str(row["reason"]),
                int(row["resulting_affinity"]),
                str(row["created_at"]),
            )
            for row in rows
        )

    def record_usage(
        self,
        settled: SettledAttempt,
        *,
        provider: str = "openai_responses",
        at: datetime | None = None,
    ) -> None:
        usage = settled.usage
        values: tuple[object | None, ...] = (
            settled.reservation.attempt_id,
            settled.reservation.period,
            provider,
            settled.reservation.model,
            usage.generation_id if usage is not None else None,
            usage.input_tokens if usage is not None else None,
            usage.cached_input_tokens if usage is not None else None,
            usage.cache_write_input_tokens if usage is not None else None,
            usage.output_tokens if usage is not None else None,
            settled.reservation.price_version,
            str(settled.reservation.twd_per_usd),
            str(settled.reservation.reserved_twd),
            str(settled.charged_twd),
            int(settled.estimated),
            _timestamp(at),
        )
        with self._write():
            try:
                self._connection.execute(
                    """
                    INSERT INTO llm_usage
                        (attempt_id, period, provider, model, generation_id, input_tokens,
                         cached_input_tokens, cache_write_input_tokens, output_tokens,
                         price_version, twd_per_usd, reserved_twd, charged_twd, estimated,
                         created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("usage attempt was already recorded") from error

    def confirmed_usage_totals(self) -> dict[str, Decimal]:
        rows = self._execute("SELECT period, charged_twd FROM llm_usage ORDER BY period").fetchall()
        totals: dict[str, Decimal] = {}
        for row in rows:
            period = str(row["period"])
            totals[period] = totals.get(period, Decimal()) + Decimal(str(row["charged_twd"]))
        return totals

    def _prepare_private_path(self) -> None:
        parent = self._path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = parent.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("database directory must be a real directory")
        parent.chmod(0o700)
        if self._path.exists() or self._path.is_symlink():
            metadata = self._path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise ValueError("database path must be a regular file")
            self._path.chmod(0o600)
            return
        file_descriptor = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(file_descriptor)

    def _configure_connection(self, busy_timeout_ms: int) -> None:
        self._connection.execute("PRAGMA foreign_keys = ON")
        journal = self._connection.execute("PRAGMA journal_mode = WAL").fetchone()
        if journal is None or str(journal[0]).lower() != "wal":
            raise RuntimeError("SQLite WAL mode is required")
        self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        self._connection.execute("PRAGMA secure_delete = ON")

    def _apply_migrations(self) -> None:
        current = self.schema_version
        latest = MIGRATIONS[-1].version if MIGRATIONS else 0
        if current > latest:
            raise RuntimeError("database schema is newer than this Lune build")
        for migration in MIGRATIONS:
            if migration.version <= current:
                continue
            self._connection.executescript(
                "BEGIN IMMEDIATE;\n"
                + migration.sql
                + f"\nPRAGMA user_version = {migration.version};\nCOMMIT;"
            )

    def _enforce_sidecar_permissions(self) -> None:
        self._path.chmod(0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self._path) + suffix)
            if sidecar.exists():
                sidecar.chmod(0o600)

    @contextmanager
    def _write(self) -> Iterator[None]:
        with self._lock:
            if self._closed:
                raise RuntimeError("memory store is closed")
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
                self._enforce_sidecar_permissions()

    def _execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            if self._closed:
                raise RuntimeError("memory store is closed")
            return self._connection.execute(sql, parameters)

    def _require_pending_turn(self, turn_id: str) -> None:
        row = self._connection.execute(
            "SELECT status FROM turns WHERE id = ?", (turn_id,)
        ).fetchone()
        if row is None or str(row[0]) != "pending":
            raise ValueError("turn is missing or no longer pending")

    def _insert_message(
        self,
        turn_id: str,
        role: str,
        content: str,
        *,
        at: datetime | None,
    ) -> str:
        clean = _validated_content(content, role)
        turn_id = _validated_id(turn_id, "turn")
        message_id = uuid4().hex
        with self._write():
            self._require_pending_turn(turn_id)
            try:
                self._connection.execute(
                    """
                    INSERT INTO messages (id, turn_id, role, content, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (message_id, turn_id, role, clean, _timestamp(at)),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError(f"turn already has a {role} message") from error
        return message_id

    def _stored_turn(self, row: sqlite3.Row) -> StoredTurn:
        message_rows = self._execute(
            """
            SELECT role, content FROM messages WHERE turn_id = ?
            ORDER BY CASE role WHEN 'user' THEN 0 ELSE 1 END
            """,
            (str(row["id"]),),
        ).fetchall()
        messages = tuple(
            ConversationMessage(role=str(item["role"]), content=str(item["content"]))  # type: ignore[arg-type]
            for item in message_rows
        )
        return StoredTurn(
            str(row["id"]),
            str(row["session_id"]),
            int(row["generation_id"]),
            int(row["sequence"]),
            messages,
        )


def _timestamp(at: datetime | None) -> str:
    value = at or datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _validated_id(value: str, label: str) -> str:
    if not value or len(value) > 128 or any(character.isspace() for character in value):
        raise ValueError(f"{label} ID must contain 1 to 128 non-whitespace characters")
    return value


def _validated_content(value: str, label: str) -> str:
    clean = value.strip()
    if not clean or len(clean) > 20_000:
        raise ValueError(f"{label} must contain 1 to 20,000 characters")
    return clean


def _normalize_content(value: str) -> str:
    return " ".join(value.casefold().split())


def _normalized_embedding(value: FloatArray) -> FloatArray:
    vector = np.asarray(value, dtype="<f4")
    if vector.shape != (EMBEDDING_DIMENSIONS,) or not np.isfinite(vector).all():
        raise ValueError("memory embedding must be a finite 384-dimensional vector")
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        raise ValueError("memory embedding cannot be zero")
    return np.asarray(vector / norm, dtype="<f4")


def _memory_from_row(row: sqlite3.Row) -> StoredMemory:
    vector = np.frombuffer(bytes(row["embedding"]), dtype="<f4").copy()
    if vector.shape != (EMBEDDING_DIMENSIONS,):
        raise RuntimeError("stored memory embedding has invalid dimensions")
    return StoredMemory(
        str(row["id"]),
        str(row["category"]),
        float(row["importance"]),
        str(row["source_turn_id"]),
        str(row["created_at"]),
        str(row["content"]),
        vector,
    )
