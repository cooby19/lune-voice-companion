from __future__ import annotations

import asyncio
import io
import json
from contextlib import suppress
from pathlib import Path

import numpy as np
import pytest
from websockets.asyncio.client import connect

from lune.config import (
    AppConfig,
    Boundaries,
    Identity,
    Language,
    PersonaKernel,
    Proactivity,
    SentenceBounds,
    Style,
)
from lune.engine import run_ui_ipc
from lune.memory.store import EMBEDDING_DIMENSIONS, MemoryStore
from lune.paths import LunePaths


def _write_ready_private_setup(paths: LunePaths) -> None:
    """Satisfy every real readiness reason so the host actually starts an engine.

    ``run_ui_ipc`` owns its ``UiRuntime`` and therefore uses the real
    ``check_readiness``.  Without a complete private setup the runtime stays in
    ``setup_required``, never builds the engine, and serves an empty thread list.
    """

    paths.ensure_private_directories()
    AppConfig().save(paths.config)
    PersonaKernel(
        identity=Identity(name="Lune", presentation="陪伴者", user_address="測試使用者"),
        language=Language(chinese_ratio=0.8),
        style=Style(traits=("溫柔",), default_sentences=SentenceBounds(min=1, max=2)),
        boundaries=Boundaries(),
        proactivity=Proactivity(level="主動"),
    ).save(paths.persona)
    # Readiness only asks whether the pinned artifacts are present on disk.
    for artifact in (
        paths.local_llm_manifest,
        paths.local_llm_runtime_python,
        paths.whisper_manifest,
        paths.e5_manifest,
    ):
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("{}", encoding="utf-8")


class HostFakeEngine:
    def __init__(self) -> None:
        self._store = MemoryStore.ephemeral()
        self._session_id = self._store.start_session("engine-thread")
        self._microphone_requested = False
        self.closed = False

    @property
    def state(self) -> str:
        return "listening" if self._microphone_requested else "mic_off"

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def store(self) -> MemoryStore:
        return self._store

    @property
    def microphone_requested(self) -> bool:
        return self._microphone_requested

    @property
    def degraded_tts(self) -> bool:
        return False

    @property
    def output_is_builtin(self) -> bool:
        return False

    async def start(self) -> str:
        return self.state

    async def set_microphone(self, enabled: bool) -> str:
        self._microphone_requested = enabled
        return self.state

    async def request_microphone_access(self) -> None:
        return None

    async def refresh_devices(self) -> str:
        return self.state

    async def submit_text(self, text: str, *, speak_text: bool = True) -> str:
        del text, speak_text
        return self.state

    def select_thread(self, thread_id: str) -> None:
        if self._store.get_conversation_thread(thread_id) is None:
            raise ValueError("unknown")
        self._session_id = thread_id

    async def close(self) -> None:
        self.closed = True
        self._store.close()


async def _wait_for_handoff(stream: io.StringIO) -> dict[str, object]:
    for _ in range(100):
        lines = stream.getvalue().splitlines()
        if lines:
            return json.loads(lines[0])
        await asyncio.sleep(0.01)
    raise AssertionError("UI child did not emit a handshake")


async def _receive_result(socket: object, request_id: str) -> dict[str, object]:
    receiver = socket  # keep the test helper independent of websocket internals
    for _ in range(20):
        message = json.loads(await receiver.recv())  # type: ignore[attr-defined]
        if message.get("type") == "result" and message.get("id") == request_id:
            return message
    raise AssertionError("did not receive expected command result")


@pytest.mark.asyncio
async def test_ui_ipc_child_hands_off_once_serves_status_and_closes_cleanly(tmp_path: Path) -> None:
    paths = LunePaths(support=tmp_path / "support", logs=tmp_path / "logs")
    _write_ready_private_setup(paths)
    fake = HostFakeEngine()
    handoff = io.StringIO()

    async def build() -> HostFakeEngine:
        return fake

    child = asyncio.create_task(
        run_ui_ipc(
            paths,
            engine_factory=build,
            handoff_stream=handoff,
            snapshot_interval_s=0.01,
            install_signal_handlers=False,
        )
    )
    payload = await _wait_for_handoff(handoff)
    url = f"ws://127.0.0.1:{payload['port']}"
    try:
        async with connect(url, compression=None) as socket:
            await socket.send(
                json.dumps(
                    {
                        "type": "hello",
                        "protocol": payload["protocol"],
                        "token": payload["token"],
                    }
                )
            )
            assert json.loads(await socket.recv())["type"] == "hello_ack"
            await socket.send(
                json.dumps(
                    {"type": "command", "id": "status", "command": "get_status", "params": {}}
                )
            )
            status = await _receive_result(socket, "status")
            assert status["type"] == "result"
            assert status["result"]["app"]["test_phase"] is True
            assert status["result"]["threads"][0]["id"] == "engine-thread"
            await socket.send(
                json.dumps({"type": "command", "id": "stop", "command": "shutdown", "params": {}})
            )
            # The reconciling pump runs on its own task, so a snapshot event
            # can land between the command and its answer.  Every other test
            # here already reads results that way.
            reply = await asyncio.wait_for(_receive_result(socket, "stop"), timeout=1.0)
            assert reply["result"]["shutdown"] is True
        # The host then closes on its own.
        assert await asyncio.wait_for(child, timeout=2.0) == 0
    finally:
        # Never leave the host task behind when an assertion above failed: an
        # abandoned task would otherwise turn any failure into this timeout.
        if not child.done():
            child.cancel()
            with suppress(asyncio.CancelledError):
                await child
    assert handoff.getvalue().count("\n") == 1
    assert fake.closed is True


async def _drain_frames(socket: object, *, quiet_for: float = 0.05) -> list[dict[str, object]]:
    """Collect frames until the socket has been quiet, so a test can start clean."""

    frames: list[dict[str, object]] = []
    while True:
        try:
            raw = await asyncio.wait_for(socket.recv(), timeout=quiet_for)  # type: ignore[attr-defined]
        except TimeoutError:
            return frames
        frames.append(json.loads(raw))


async def _collect_events(socket: object, count: int) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for _ in range(40):
        message = json.loads(await asyncio.wait_for(socket.recv(), timeout=2.0))  # type: ignore[attr-defined]
        if message.get("type") == "event":
            events.append(message)
            if len(events) >= count:
                return events
    raise AssertionError(f"did not receive {count} events, saw {events}")


@pytest.mark.asyncio
async def test_a_turn_reaches_the_authenticated_ui_as_events_not_a_whole_snapshot(
    tmp_path: Path,
) -> None:
    """End to end, one turn must cost its own two messages and nothing else.

    The reconciliation interval is set far beyond the test's lifetime, so any
    frame observed after the seeding ``get_status`` is an incremental event.
    """

    paths = LunePaths(support=tmp_path / "support", logs=tmp_path / "logs")
    _write_ready_private_setup(paths)
    fake = HostFakeEngine()
    handoff = io.StringIO()

    async def build() -> HostFakeEngine:
        return fake

    child = asyncio.create_task(
        run_ui_ipc(
            paths,
            engine_factory=build,
            handoff_stream=handoff,
            snapshot_interval_s=30.0,
            install_signal_handlers=False,
        )
    )
    payload = await _wait_for_handoff(handoff)
    url = f"ws://127.0.0.1:{payload['port']}"
    try:
        async with connect(url, compression=None) as socket:
            await socket.send(
                json.dumps(
                    {
                        "type": "hello",
                        "protocol": payload["protocol"],
                        "token": payload["token"],
                    }
                )
            )
            assert json.loads(await socket.recv())["type"] == "hello_ack"
            await socket.send(
                json.dumps({"type": "command", "id": "seed", "command": "get_status", "params": {}})
            )
            await _receive_result(socket, "seed")
            await _drain_frames(socket)

            turn_id = fake.store.begin_turn("engine-thread", 1)
            fake.store.accept_user_transcript(turn_id, "使用者說的話")
            fake.store.append_assistant_playback(turn_id, "Lune 回的話")
            fake.store.complete_turn(turn_id)

            events = await _collect_events(socket, 3)
            assert [event["event"] for event in events] == [
                "message_added",
                "message_added",
                "thread_updated",
            ]
            messages = [event["payload"]["message"] for event in events[:2]]
            assert [message["role"] for message in messages] == ["user", "assistant"]
            assert [message["content"] for message in messages] == ["使用者說的話", "Lune 回的話"]
            assert all(message["thread_id"] == "engine-thread" for message in messages)
            assert events[2]["payload"]["thread"]["id"] == "engine-thread"
            # Nothing re-sent the whole state to say what the events already did.
            assert [frame["event"] for frame in await _drain_frames(socket)] == []

            await socket.send(
                json.dumps({"type": "command", "id": "stop", "command": "shutdown", "params": {}})
            )
            await _receive_result(socket, "stop")
        assert await asyncio.wait_for(child, timeout=2.0) == 0
    finally:
        if not child.done():
            child.cancel()
            with suppress(asyncio.CancelledError):
                await child


@pytest.mark.asyncio
async def test_a_forgotten_memory_reaches_the_ui_as_a_memory_event(tmp_path: Path) -> None:
    paths = LunePaths(support=tmp_path / "support", logs=tmp_path / "logs")
    _write_ready_private_setup(paths)
    fake = HostFakeEngine()
    turn_id = fake.store.begin_turn("engine-thread", 1)
    fake.store.accept_user_transcript(turn_id, "使用者說的話")
    fake.store.append_assistant_playback(turn_id, "Lune 回的話")
    fake.store.complete_turn(turn_id)
    vector = np.zeros(EMBEDDING_DIMENSIONS, dtype=np.float32)
    vector[0] = 1.0
    fake.store.add_memory(
        memory_id="memory-one",
        content="她記得的事",
        category="stable_preference",
        importance=0.6,
        embedding=vector,
        embedding_model="test-model",
        embedding_revision="test-revision",
        source_turn_id=turn_id,
    )
    handoff = io.StringIO()

    async def build() -> HostFakeEngine:
        return fake

    child = asyncio.create_task(
        run_ui_ipc(
            paths,
            engine_factory=build,
            handoff_stream=handoff,
            snapshot_interval_s=30.0,
            install_signal_handlers=False,
        )
    )
    payload = await _wait_for_handoff(handoff)
    url = f"ws://127.0.0.1:{payload['port']}"
    try:
        async with connect(url, compression=None) as socket:
            await socket.send(
                json.dumps(
                    {
                        "type": "hello",
                        "protocol": payload["protocol"],
                        "token": payload["token"],
                    }
                )
            )
            assert json.loads(await socket.recv())["type"] == "hello_ack"
            await socket.send(
                json.dumps({"type": "command", "id": "seed", "command": "get_status", "params": {}})
            )
            await _receive_result(socket, "seed")
            await _drain_frames(socket)

            await socket.send(
                json.dumps(
                    {
                        "type": "command",
                        "id": "forget",
                        "command": "forget_memory",
                        "params": {"memory_id": "memory-one", "confirmation": "memory-one"},
                    }
                )
            )
            events = await _collect_events(socket, 1)
            assert events[0]["event"] == "memory_updated"
            assert events[0]["payload"]["memories"] == []

            await socket.send(
                json.dumps({"type": "command", "id": "stop", "command": "shutdown", "params": {}})
            )
            await _receive_result(socket, "stop")
        assert await asyncio.wait_for(child, timeout=2.0) == 0
    finally:
        if not child.done():
            child.cancel()
            with suppress(asyncio.CancelledError):
                await child
