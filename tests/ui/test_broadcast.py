"""How the one broadcasting task splits work between events and the snapshot.

The snapshot used to be the only channel, recomputed and re-sent five times a
second.  These tests pin the replacement: events carry the change, and the
whole snapshot goes out only for what events cannot express — or to correct a
client whose events were dropped.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from lune.config import AppConfig
from lune.engine import _broadcast_ui_state
from lune.ipc.contracts import JSONValue
from lune.ipc.server import BroadcastResult
from lune.memory.store import MemoryStore
from lune.paths import LunePaths
from lune.readiness import Readiness
from lune.ui.runtime import UiRuntime
from tests.ui.test_runtime import FakeEngine

RECONCILE_S = 0.02


class FakeBroadcastServer:
    """Record what reached the wire, in the order the pump produced it.

    ``clients`` is how many authenticated peers the real server would have.
    Zero is the state the shell starts in: the pump is already running while
    the WebView is still authenticating.
    """

    def __init__(self, clients: int = 1) -> None:
        self.frames: list[tuple[str, dict[str, JSONValue]]] = []
        self.running = True
        self.clients = clients

    async def broadcast(self, event: str, payload: object) -> BroadcastResult:
        assert isinstance(payload, dict)
        self.frames.append((event, payload))
        return BroadcastResult(attempted=self.clients, delivered=self.clients, dropped=0)

    def names(self) -> list[str]:
        return [name for name, _payload in self.frames]


def _ready(_paths: LunePaths) -> Readiness:
    return Readiness("mic_off", ())


def _complete_turn(store: MemoryStore, thread_id: str, *, user: str, assistant: str) -> None:
    turn_id = store.begin_turn(thread_id, 1)
    store.accept_user_transcript(turn_id, user)
    store.append_assistant_playback(turn_id, assistant)
    store.complete_turn(turn_id)


async def _wait_for_frames(server: FakeBroadcastServer, count: int) -> None:
    for _ in range(200):
        if len(server.frames) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {count} frames, saw {server.names()}")


class Harness:
    def __init__(
        self,
        runtime: UiRuntime,
        engine: FakeEngine,
        server: FakeBroadcastServer,
        events: asyncio.Queue[tuple[str, dict[str, JSONValue]]],
    ) -> None:
        self.runtime = runtime
        self.engine = engine
        self.server = server
        self.events = events
        self.overflowed = False
        self.task: asyncio.Task[None] | None = None

    @property
    def store(self) -> MemoryStore:
        assert self.engine.store is not None
        return self.engine.store

    def drain_overflow(self) -> bool:
        dropped = self.overflowed
        self.overflowed = False
        return dropped

    def start(self, reconcile_s: float = RECONCILE_S) -> None:
        self.task = asyncio.create_task(
            _broadcast_ui_state(
                self.server,  # type: ignore[arg-type]
                self.runtime,
                self.events,
                self.drain_overflow,
                reconcile_s,
            )
        )

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await self.task
        await self.runtime.close()


async def _harness(tmp_path: Path, *, capacity: int = 64) -> Harness:
    paths = LunePaths(support=tmp_path / "support", logs=tmp_path / "logs")
    AppConfig().save(paths.config)
    engine = FakeEngine()

    async def build() -> FakeEngine:
        return engine

    events: asyncio.Queue[tuple[str, dict[str, JSONValue]]] = asyncio.Queue(maxsize=capacity)
    harness: Harness | None = None

    def publish(name: str, payload: dict[str, JSONValue]) -> None:
        assert harness is not None
        try:
            events.put_nowait((name, payload))
        except asyncio.QueueFull:
            harness.overflowed = True

    runtime = UiRuntime(paths, build, readiness_checker=_ready, event_sink=publish)
    await runtime.start()
    harness = Harness(runtime, engine, FakeBroadcastServer(), events)
    return harness


@pytest.mark.asyncio
async def test_a_turn_costs_three_events_and_no_extra_snapshot(tmp_path: Path) -> None:
    """The regression this change exists to prevent."""

    harness = await _harness(tmp_path)
    harness.start()
    try:
        # The pump opens with one snapshot so a client that never asked for
        # status still has a complete starting point.
        await _wait_for_frames(harness.server, 1)
        assert harness.server.names() == ["snapshot"]

        _complete_turn(harness.store, "thread-one", user="使用者說的話", assistant="Lune 回的話")
        await _wait_for_frames(harness.server, 4)

        assert harness.server.names()[1:] == [
            "message_added",
            "message_added",
            "thread_updated",
        ]
        # Sending the events advanced the reconciliation baseline, so the ticks
        # that follow have nothing left to correct.
        await asyncio.sleep(RECONCILE_S * 8)
        assert len(harness.server.frames) == 4
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_a_snapshot_nobody_received_is_not_a_baseline(tmp_path: Path) -> None:
    """The frame that goes out before the WebView has authenticated.

    A setup screen produces no further change to broadcast, so counting that
    unheard frame as the baseline left the client that connected a moment
    later with no state at all — on screen, a permanent
    「正在取得 Lune 的目前狀態…」 capsule over the setup card.
    """

    harness = await _harness(tmp_path)
    harness.server.clients = 0
    harness.start()
    try:
        # Unheard, so the pump keeps offering the same state.
        await _wait_for_frames(harness.server, 3)
        assert set(harness.server.names()) == {"snapshot"}

        harness.server.clients = 1
        await _wait_for_frames(harness.server, len(harness.server.frames) + 1)
        # And once it lands the baseline holds again: no needless repeats.
        settled = len(harness.server.frames)
        await asyncio.sleep(RECONCILE_S * 8)
        assert len(harness.server.frames) == settled
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_the_snapshot_still_carries_what_no_event_describes(tmp_path: Path) -> None:
    """State outside the store has no event of its own and must still arrive."""

    harness = await _harness(tmp_path)
    harness.start()
    try:
        await _wait_for_frames(harness.server, 1)

        await harness.runtime.handle("set_microphone", {"enabled": True})
        await _wait_for_frames(harness.server, 2)

        event, payload = harness.server.frames[-1]
        assert event == "snapshot"
        assert payload["app"]["state"] == "listening"
        assert payload["call"]["active"] is True
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_a_dropped_event_is_corrected_by_the_next_snapshot(tmp_path: Path) -> None:
    """Overflow may cost latency; it must never leave the client wrong."""

    harness = await _harness(tmp_path, capacity=1)
    harness.start()
    try:
        await _wait_for_frames(harness.server, 1)
        settled = len(harness.server.frames)

        # One completed turn produces three events against a queue of one, so
        # the sink has to drop rather than wait on the pump.
        _complete_turn(harness.store, "thread-one", user="使用者說的話", assistant="Lune 回的話")
        assert harness.overflowed is True

        await _wait_for_frames(harness.server, settled + 1)
        assert "snapshot" in harness.server.names()[settled:]
        recovered = [
            payload
            for event, payload in harness.server.frames
            if event == "snapshot" and payload["messages"]
        ]
        assert recovered
        assert [message["content"] for message in recovered[-1]["messages"]] == [
            "使用者說的話",
            "Lune 回的話",
        ]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_a_full_queue_never_blocks_the_write_that_caused_it(tmp_path: Path) -> None:
    """The store notifies from the pipeline's own task, so the sink cannot wait."""

    harness = await _harness(tmp_path, capacity=1)
    # Deliberately no pump: nothing drains the queue at all.
    try:
        for index in range(12):
            _complete_turn(
                harness.store,
                "thread-one",
                user=f"第 {index} 輪的使用者",
                assistant=f"第 {index} 輪的回覆",
            )

        assert harness.overflowed is True
        assert harness.events.qsize() == 1
        # Every write still committed, which is the property that matters.
        assert len(harness.store.conversation_messages("thread-one")) == 24
    finally:
        await harness.close()
