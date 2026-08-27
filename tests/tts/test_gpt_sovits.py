from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from lune.tts.contracts import PCMChunk, TTSBackendError, TTSRequest
from lune.tts.gpt_sovits import (
    GPTSoVITSBackend,
    SandboxedGPTWorkerLauncher,
    build_worker_profile,
)
from lune.tts.protocol import ControlFrame, PCMFrame, WorkerFrame, WorkerProtocolError
from lune.tts_spike.manifest import PrivateManifestCheck
from lune.tts_spike.sandbox import SandboxCheck


class FakeConnection:
    def __init__(self, *frames: WorkerFrame | Exception, pid: int = 1234) -> None:
        self._frames: asyncio.Queue[WorkerFrame | Exception] = asyncio.Queue()
        for frame in frames:
            self._frames.put_nowait(frame)
        self._pid = pid
        self.sent: list[ControlFrame] = []
        self._sent_condition = asyncio.Condition()
        self.terminated = False
        self.closed = False

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def alive(self) -> bool:
        return not self.terminated and not self.closed

    async def send(self, frame: ControlFrame) -> None:
        async with self._sent_condition:
            self.sent.append(frame)
            self._sent_condition.notify_all()

    async def wait_for_sent(self, count: int) -> None:
        async with self._sent_condition:
            await self._sent_condition.wait_for(lambda: len(self.sent) >= count)

    async def receive(self) -> WorkerFrame:
        value = await self._frames.get()
        if isinstance(value, Exception):
            raise value
        return value

    def queue(self, frame: WorkerFrame | Exception) -> None:
        self._frames.put_nowait(frame)

    async def terminate(self) -> None:
        self.terminated = True
        self._frames.put_nowait(WorkerProtocolError("worker_eof"))

    async def close(self) -> None:
        self.closed = True


class FakeLauncher:
    def __init__(self, *results: FakeConnection | TTSBackendError) -> None:
        self.results = list(results)
        self.launches = 0

    async def launch(self) -> FakeConnection:
        self.launches += 1
        result = self.results.pop(0)
        if isinstance(result, TTSBackendError):
            raise result
        return result


@pytest.mark.asyncio
async def test_gpt_backend_streams_in_order_pcm() -> None:
    connection = FakeConnection(
        PCMFrame(0, PCMChunk(8, 32_000, 1, b"\x01\x00")),
        PCMFrame(1, PCMChunk(8, 32_000, 1, b"\x02\x00")),
        ControlFrame(type="done", request_id="r", generation_id=8, sequence=2, code="complete"),
    )
    backend = GPTSoVITSBackend(FakeLauncher(connection))

    chunks = [chunk async for chunk in backend.synthesize(TTSRequest("r", 8, "hello"))]

    assert chunks == [
        PCMChunk(8, 32_000, 1, b"\x01\x00"),
        PCMChunk(8, 32_000, 1, b"\x02\x00"),
    ]
    assert connection.sent[0].type == "synthesize"
    assert "hello" not in repr(connection.sent[0])


@pytest.mark.asyncio
async def test_out_of_order_pcm_fails_protocol_and_discards_worker() -> None:
    connection = FakeConnection(PCMFrame(1, PCMChunk(1, 32_000, 1, b"\x01\x00")))
    backend = GPTSoVITSBackend(FakeLauncher(connection))

    with pytest.raises(TTSBackendError, match="protocol_error"):
        _ = [chunk async for chunk in backend.synthesize(TTSRequest("r", 1, "hello"))]

    assert connection.terminated and connection.closed
    assert backend.circuit_breaker.consecutive_failures == 1


@pytest.mark.asyncio
async def test_worker_crash_then_rebuild_failure_opens_circuit() -> None:
    crashed = FakeConnection(WorkerProtocolError("worker_eof"))
    launcher = FakeLauncher(crashed, TTSBackendError("backend_unavailable"))
    backend = GPTSoVITSBackend(launcher)

    with pytest.raises(TTSBackendError):
        _ = [chunk async for chunk in backend.synthesize(TTSRequest("a", 1, "hello"))]
    with pytest.raises(TTSBackendError):
        _ = [chunk async for chunk in backend.synthesize(TTSRequest("b", 2, "hello"))]

    assert launcher.launches == 2
    assert backend.circuit_breaker.state == "open"


@pytest.mark.asyncio
async def test_generation_cancel_drops_late_pcm() -> None:
    connection = FakeConnection()
    backend = GPTSoVITSBackend(FakeLauncher(connection), cancel_timeout_seconds=0.1)
    chunks: list[PCMChunk] = []

    async def collect() -> None:
        with pytest.raises(TTSBackendError, match="cancelled"):
            async for chunk in backend.synthesize(TTSRequest("r", 5, "hello")):
                chunks.append(chunk)

    synthesis = asyncio.create_task(collect())
    await connection.wait_for_sent(1)
    cancellation = asyncio.create_task(backend.cancel(5))
    await connection.wait_for_sent(2)
    connection.queue(PCMFrame(0, PCMChunk(5, 32_000, 1, b"\x01\x00")))
    connection.queue(
        ControlFrame(type="done", request_id="r", generation_id=5, sequence=1, code="cancelled")
    )

    await cancellation
    await synthesis
    assert chunks == []
    assert connection.sent[1] == ControlFrame(type="cancel", generation_id=5)


@pytest.mark.asyncio
async def test_cancel_timeout_terminates_only_current_worker_pid() -> None:
    connection = FakeConnection(pid=777)
    backend = GPTSoVITSBackend(FakeLauncher(connection), cancel_timeout_seconds=0.01)

    async def collect() -> None:
        with pytest.raises(TTSBackendError, match="cancelled"):
            _ = [chunk async for chunk in backend.synthesize(TTSRequest("r", 6, "hello"))]

    synthesis = asyncio.create_task(collect())
    await connection.wait_for_sent(1)
    await backend.cancel(6)
    await synthesis

    assert connection.terminated
    assert backend.worker_pid is None


def test_sandbox_profile_is_deny_by_default_and_network_closed(tmp_path: Path) -> None:
    profile = build_worker_profile(
        runtime_root=tmp_path / "runtime",
        voice_root=tmp_path / "voice",
        temp_root=tmp_path / "temp",
        python_root=tmp_path / "python",
        worker_root=tmp_path / "worker",
    )
    assert "(deny default)" in profile
    assert "(deny network*)" in profile
    assert "(allow file-write*" in profile
    assert str(tmp_path / "voice") in profile


@pytest.mark.asyncio
async def test_sandbox_denial_fails_closed_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o755)
    manifest = tmp_path / "voice" / "manifest.json"
    manifest.parent.mkdir()
    manifest.write_text("{}", encoding="utf-8")
    manifest.chmod(0o600)
    monkeypatch.setattr(
        "lune.tts.gpt_sovits.check_private_manifest",
        lambda **_kwargs: PrivateManifestCheck("ready", object()),
    )
    monkeypatch.setattr(
        "lune.tts.gpt_sovits.probe_sandbox",
        lambda **_kwargs: SandboxCheck("probe_denial_failed"),
    )
    launcher = SandboxedGPTWorkerLauncher(
        python_executable=Path(sys.executable),
        runtime_root=runtime,
        manifest_path=manifest,
    )

    with pytest.raises(TTSBackendError, match="backend_unavailable"):
        await launcher.launch()


def test_worker_environment_is_allowlisted_without_api_keys(tmp_path: Path) -> None:
    launcher = SandboxedGPTWorkerLauncher(
        python_executable=Path(sys.executable),
        runtime_root=tmp_path / "runtime",
        manifest_path=tmp_path / "voice" / "manifest.json",
    )
    environment = launcher._environment(tmp_path / "temp", tmp_path / "voice")

    assert "OPENAI_API_KEY" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["TRANSFORMERS_OFFLINE"] == "1"
