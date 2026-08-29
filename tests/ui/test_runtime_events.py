"""The incremental UI events a committed store change turns into.

Every payload here is checked against the shape the same runtime puts in a
whole snapshot.  The two channels feed one merge function in ``app.js``, so a
field renamed on one side and not the other is the failure this file exists to
catch.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

import lune.ui
from lune.config import AppConfig
from lune.ipc.contracts import UI_EVENT_NAMES, JSONValue, event_message
from lune.memory.store import EMBEDDING_DIMENSIONS, MemoryStore
from lune.paths import LunePaths
from lune.readiness import Readiness
from lune.ui.runtime import UiRuntime
from tests.ui.test_runtime import FakeEngine


def _ready(_paths: LunePaths) -> Readiness:
    return Readiness("mic_off", ())


class RecordingRuntime:
    """A runtime whose events are captured instead of broadcast."""

    def __init__(self, runtime: UiRuntime, engine: FakeEngine) -> None:
        self.runtime = runtime
        self.engine = engine
        self.events: list[tuple[str, dict[str, JSONValue]]] = []

    @property
    def store(self) -> MemoryStore:
        assert self.engine.store is not None
        return self.engine.store

    def names(self) -> list[str]:
        return [name for name, _payload in self.events]

    def payloads(self, name: str) -> list[dict[str, JSONValue]]:
        return [payload for event, payload in self.events if event == name]


async def _recording(tmp_path: Path) -> RecordingRuntime:
    paths = LunePaths(support=tmp_path / "support", logs=tmp_path / "logs")
    AppConfig().save(paths.config)
    engine = FakeEngine()

    async def build() -> FakeEngine:
        return engine

    events: list[tuple[str, dict[str, JSONValue]]] = []
    runtime = UiRuntime(
        paths,
        build,
        readiness_checker=_ready,
        event_sink=lambda name, payload: events.append((name, payload)),
    )
    await runtime.start()
    recording = RecordingRuntime(runtime, engine)
    recording.events = events
    return recording


def _embedding(index: int = 0) -> np.ndarray[tuple[int], np.dtype[np.float32]]:
    vector = np.zeros(EMBEDDING_DIMENSIONS, dtype=np.float32)
    vector[index] = 1.0
    return vector


def _complete_turn(store: MemoryStore, thread_id: str, *, user: str, assistant: str) -> str:
    turn_id = store.begin_turn(thread_id, 1)
    store.accept_user_transcript(turn_id, user)
    store.append_assistant_playback(turn_id, assistant)
    store.complete_turn(turn_id)
    return turn_id


@pytest.mark.asyncio
async def test_every_event_the_runtime_emits_is_allowlisted_and_fits_one_frame(
    tmp_path: Path,
) -> None:
    """The transport, not this test, is the authority on names and bounds."""

    recording = await _recording(tmp_path)
    try:
        await recording.runtime.handle("create_thread", {})
        thread_id = str(recording.runtime.snapshot()["active_thread_id"])
        turn_id = _complete_turn(
            recording.store, thread_id, user="使用者說的話", assistant="Lune 回的話"
        )
        recording.store.add_memory(
            memory_id="memory-one",
            content="記得的事",
            category="stable_preference",
            importance=0.6,
            embedding=_embedding(),
            embedding_model="test-model",
            embedding_revision="test-revision",
            source_turn_id=turn_id,
        )

        assert recording.events
        for name, payload in recording.events:
            assert name in UI_EVENT_NAMES
            # Raises rather than truncating if a payload outgrows the frame.
            event_message(name, payload, allowed_events=UI_EVENT_NAMES)
    finally:
        await recording.runtime.close()


@pytest.mark.asyncio
async def test_a_new_thread_is_announced_in_the_shape_the_snapshot_uses(
    tmp_path: Path,
) -> None:
    recording = await _recording(tmp_path)
    try:
        await recording.runtime.handle("create_thread", {})

        snapshot = recording.runtime.snapshot()
        thread_id = str(snapshot["active_thread_id"])
        announced = [
            payload["thread"]
            for payload in recording.payloads("thread_updated")
            if payload["thread"]["id"] == thread_id
        ]
        assert len(announced) == 1
        # Identical to the snapshot entry, so merging one cannot leave the
        # client with a differently shaped thread than reconciling does.
        matching = [thread for thread in snapshot["threads"] if thread["id"] == thread_id]
        assert announced == matching
    finally:
        await recording.runtime.close()


@pytest.mark.asyncio
async def test_renaming_a_thread_announces_the_new_title(tmp_path: Path) -> None:
    recording = await _recording(tmp_path)
    try:
        await recording.runtime.handle(
            "rename_thread", {"thread_id": "thread-one", "title": "週末安排"}
        )

        threads = [payload["thread"] for payload in recording.payloads("thread_updated")]
        assert threads[-1]["id"] == "thread-one"
        assert threads[-1]["title"] == "週末安排"
        assert threads[-1]["title_source"] == "manual"
    finally:
        await recording.runtime.close()


@pytest.mark.asyncio
async def test_a_completed_turn_announces_only_its_own_two_messages(tmp_path: Path) -> None:
    """This is the change that used to cost a whole snapshot of private text."""

    recording = await _recording(tmp_path)
    try:
        _complete_turn(
            recording.store, "thread-one", user="第一輪的使用者", assistant="第一輪的回覆"
        )
        recording.events.clear()
        _complete_turn(
            recording.store, "thread-one", user="第二輪的使用者", assistant="第二輪的回覆"
        )

        messages = [payload["message"] for payload in recording.payloads("message_added")]
        assert [message["role"] for message in messages] == ["user", "assistant"]
        assert [message["content"] for message in messages] == ["第二輪的使用者", "第二輪的回覆"]
        # The first turn's text is not re-sent, and every message names the
        # thread `app.js` files it under.
        assert all(message["thread_id"] == "thread-one" for message in messages)
        assert "第一輪的使用者" not in str(recording.events)
        # The thread's place in the recency-ordered index moved as well.
        assert "thread_updated" in recording.names()
    finally:
        await recording.runtime.close()


@pytest.mark.asyncio
async def test_an_announced_message_matches_the_snapshot_entry_for_it(tmp_path: Path) -> None:
    recording = await _recording(tmp_path)
    try:
        _complete_turn(recording.store, "thread-one", user="使用者說的話", assistant="Lune 回的話")

        announced = [payload["message"] for payload in recording.payloads("message_added")]
        assert announced == recording.runtime.snapshot()["messages"]
    finally:
        await recording.runtime.close()


@pytest.mark.asyncio
async def test_a_turn_in_a_background_thread_is_announced_under_that_thread(
    tmp_path: Path,
) -> None:
    """A call can run in one thread while the user reads another one."""

    recording = await _recording(tmp_path)
    try:
        created = await recording.runtime.handle("create_thread", {})
        background = str(created["active_thread_id"])
        await recording.runtime.handle("select_thread", {"thread_id": "thread-one"})
        recording.events.clear()

        _complete_turn(recording.store, background, user="背景的問題", assistant="背景的回答")

        messages = [payload["message"] for payload in recording.payloads("message_added")]
        assert messages and all(message["thread_id"] == background for message in messages)
        # The snapshot only ever carries the selected thread, so without the
        # event this text would not reach the client at all.
        assert recording.runtime.snapshot()["messages"] == []
    finally:
        await recording.runtime.close()


@pytest.mark.asyncio
async def test_remembering_and_forgetting_announce_the_bounded_memory_list(
    tmp_path: Path,
) -> None:
    recording = await _recording(tmp_path)
    try:
        turn_id = _complete_turn(
            recording.store, "thread-one", user="使用者說的話", assistant="Lune 回的話"
        )
        recording.store.add_memory(
            memory_id="memory-one",
            content="她記得的事",
            category="stable_preference",
            importance=0.6,
            embedding=_embedding(),
            embedding_model="test-model",
            embedding_revision="test-revision",
            source_turn_id=turn_id,
        )
        added = recording.payloads("memory_updated")[-1]
        assert added["memories"] == recording.runtime.snapshot()["memories"]
        assert [memory["id"] for memory in added["memories"]] == ["memory-one"]

        recording.events.clear()
        await recording.runtime.handle(
            "forget_memory", {"memory_id": "memory-one", "confirmation": "memory-one"}
        )

        forgotten = recording.payloads("memory_updated")[-1]
        assert forgotten["memories"] == []
    finally:
        await recording.runtime.close()


@pytest.mark.asyncio
async def test_a_runtime_without_a_sink_still_serves_snapshots(tmp_path: Path) -> None:
    """The sink is optional; a store change must not require one."""

    paths = LunePaths(support=tmp_path / "support", logs=tmp_path / "logs")
    AppConfig().save(paths.config)
    engine = FakeEngine()

    async def build() -> FakeEngine:
        return engine

    runtime = UiRuntime(paths, build, readiness_checker=_ready)
    await runtime.start()
    try:
        assert engine.store is not None
        _complete_turn(engine.store, "thread-one", user="使用者說的話", assistant="Lune 回的話")

        assert len(runtime.snapshot()["messages"]) == 2
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_closing_the_runtime_detaches_before_the_store_goes_away(tmp_path: Path) -> None:
    """Teardown order matters: the store outlives the runtime by one call.

    A listener still attached while the engine closes would read rows that
    nothing is left to broadcast, so detaching has to happen first.
    """

    recording = await _recording(tmp_path)
    engine = recording.engine
    store = recording.store
    engine_close = engine.close
    during_close: list[str] = []

    async def close_and_probe() -> None:
        recording.events.clear()
        # The store is still open at this point, so a listener that survived
        # the runtime would announce this write.
        store.start_session("thread-during-close")
        during_close.extend(recording.names())
        await engine_close()

    engine.close = close_and_probe  # type: ignore[method-assign]
    await recording.runtime.close()

    assert during_close == []


def _merge_event_cases() -> set[str]:
    """The event names ``app.js`` actually merges, read out of the shipped file.

    Parsing the real static asset is the point.  A name that exists only in the
    Python allowlist is the dead frontend branch this change was made to
    remove, and it would come back silently.
    """

    source = (Path(lune.ui.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    start = source.index("function mergeEvent(")
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                body = source[opening : index + 1]
                break
    else:
        raise AssertionError("mergeEvent() is not brace balanced")
    return set(re.findall(r'case "([a-z_]+)":', body))


@pytest.mark.asyncio
async def test_app_js_merges_every_event_the_runtime_can_emit(tmp_path: Path) -> None:
    """The two sides of the contract, checked against each other.

    ``UI_EVENT_NAMES`` allowlists more than this: `budget_changed` has neither
    a sender nor a handler.  The claim here is narrower and the one that
    matters — nothing the engine sends lands in `mergeEvent`\'s default branch.
    """

    recording = await _recording(tmp_path)
    try:
        await recording.runtime.handle("create_thread", {})
        thread_id = str(recording.runtime.snapshot()["active_thread_id"])
        turn_id = _complete_turn(
            recording.store, thread_id, user="使用者說的話", assistant="Lune 回的話"
        )
        recording.store.add_memory(
            memory_id="memory-one",
            content="記得的事",
            category="stable_preference",
            importance=0.6,
            embedding=_embedding(),
            embedding_model="test-model",
            embedding_revision="test-revision",
            source_turn_id=turn_id,
        )

        emitted = set(recording.names())
        assert emitted == {"thread_updated", "message_added", "memory_updated"}
        assert emitted <= _merge_event_cases()
    finally:
        await recording.runtime.close()
