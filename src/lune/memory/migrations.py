"""Versioned SQLite schema migrations for private local state."""

from __future__ import annotations

from typing import Final, NamedTuple


class Migration(NamedTuple):
    version: int
    sql: str


MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(
        1,
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            ended_at TEXT
        );

        CREATE TABLE turns (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            generation_id INTEGER NOT NULL CHECK (generation_id >= 0),
            sequence INTEGER NOT NULL CHECK (sequence > 0),
            status TEXT NOT NULL CHECK (status IN ('pending', 'complete', 'cancelled')),
            started_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE (session_id, sequence)
        );

        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL CHECK (length(trim(content)) > 0),
            created_at TEXT NOT NULL,
            UNIQUE (turn_id, role)
        );

        CREATE TABLE summaries (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id) ON DELETE CASCADE,
            start_turn_sequence INTEGER NOT NULL CHECK (start_turn_sequence > 0),
            end_turn_sequence INTEGER NOT NULL CHECK (
                end_turn_sequence >= start_turn_sequence
            ),
            covered_turn_count INTEGER NOT NULL CHECK (covered_turn_count > 0),
            content TEXT NOT NULL CHECK (length(trim(content)) > 0),
            updated_at TEXT NOT NULL
        );

        CREATE TABLE long_term_memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL CHECK (length(trim(content)) > 0),
            normalized_content TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL,
            importance REAL NOT NULL CHECK (importance >= 0.0 AND importance <= 1.0),
            embedding BLOB NOT NULL,
            embedding_dimensions INTEGER NOT NULL CHECK (embedding_dimensions = 384),
            embedding_model TEXT NOT NULL,
            embedding_revision TEXT NOT NULL,
            embedding_dtype TEXT NOT NULL CHECK (embedding_dtype = 'float32'),
            source_turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE relationship_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            affinity INTEGER NOT NULL CHECK (affinity >= 0 AND affinity <= 100),
            updated_at TEXT NOT NULL
        );

        CREATE TABLE relationship_events (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE RESTRICT,
            turn_id TEXT NOT NULL UNIQUE REFERENCES turns(id) ON DELETE RESTRICT,
            generation_id INTEGER NOT NULL CHECK (generation_id >= 0),
            delta INTEGER NOT NULL CHECK (delta IN (-1, 1)),
            reason TEXT NOT NULL,
            resulting_affinity INTEGER NOT NULL CHECK (
                resulting_affinity >= 0 AND resulting_affinity <= 100
            ),
            created_at TEXT NOT NULL
        );

        CREATE TABLE llm_usage (
            attempt_id TEXT PRIMARY KEY,
            period TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            generation_id INTEGER,
            input_tokens INTEGER,
            cached_input_tokens INTEGER,
            cache_write_input_tokens INTEGER,
            output_tokens INTEGER,
            price_version TEXT NOT NULL,
            twd_per_usd TEXT NOT NULL,
            reserved_twd TEXT NOT NULL,
            charged_twd TEXT NOT NULL,
            estimated INTEGER NOT NULL CHECK (estimated IN (0, 1)),
            created_at TEXT NOT NULL
        );

        CREATE INDEX turns_session_status_sequence
            ON turns(session_id, status, sequence);
        CREATE INDEX messages_turn_role ON messages(turn_id, role);
        CREATE INDEX relationship_events_session ON relationship_events(session_id);
        CREATE INDEX llm_usage_period ON llm_usage(period);

        INSERT INTO relationship_state (id, affinity, updated_at)
        VALUES (1, 50, '1970-01-01T00:00:00+00:00');
        """,
    ),
    Migration(
        2,
        """
        ALTER TABLE sessions ADD COLUMN title TEXT NOT NULL DEFAULT '新對話'
            CHECK (length(trim(title)) > 0);
        ALTER TABLE sessions ADD COLUMN title_source TEXT NOT NULL DEFAULT 'default'
            CHECK (title_source IN ('default', 'generated', 'manual'));
        ALTER TABLE sessions ADD COLUMN updated_at TEXT NOT NULL
            DEFAULT '1970-01-01T00:00:00+00:00';
        UPDATE sessions
        SET updated_at = COALESCE(ended_at, started_at)
        WHERE updated_at = '1970-01-01T00:00:00+00:00';

        ALTER TABLE long_term_memories ADD COLUMN source TEXT NOT NULL DEFAULT 'lune_observed'
            CHECK (source IN ('user_requested', 'lune_observed'));
        UPDATE long_term_memories
        SET source = CASE
            WHEN category = 'explicit_request' THEN 'user_requested'
            ELSE 'lune_observed'
        END;

        CREATE INDEX sessions_updated_at ON sessions(updated_at DESC, id DESC);
        """,
    ),
    Migration(
        3,
        """
        CREATE TABLE turn_retrieved_memories (
            turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
            memory_id TEXT NOT NULL REFERENCES long_term_memories(id) ON DELETE CASCADE,
            position INTEGER NOT NULL CHECK (position >= 0),
            PRIMARY KEY (turn_id, memory_id)
        );

        CREATE INDEX turn_retrieved_memories_memory
            ON turn_retrieved_memories(memory_id);
        """,
    ),
)
