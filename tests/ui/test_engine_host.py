from __future__ import annotations

import asyncio
import io
import json
from contextlib import suppress
from pathlib import Path

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
from lune.memory.store import MemoryStore
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
            reply = json.loads(await asyncio.wait_for(socket.recv(), timeout=0.5))
            assert reply["type"] == "result"
            assert reply["id"] == "stop"
            assert reply["result"]["shutdown"] is True
        # The shutdown result is the last frame; the host then closes on its own.
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
