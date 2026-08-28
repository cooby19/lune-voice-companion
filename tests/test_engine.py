from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path

import numpy as np
import pytest

from lune.audio.coreaudio import StreamOwnerHealth
from lune.audio.devices import DeviceInfo, DeviceSnapshot
from lune.audio.transport import LocalAudioTransport
from lune.engine import EngineDependencies, compose_voice_engine
from lune.llm.budget import BudgetLedger
from lune.llm.contracts import (
    GenerationLLMTextFrame,
    ModelName,
    ProviderStreamFrame,
    ProviderTerminalFrame,
)
from lune.llm.streaming import ScriptedAttemptProvider
from lune.memory.embedding import E5MemoryRetriever
from lune.memory.store import MemoryStore
from lune.stt.contracts import STTEvent
from lune.tts.contracts import PCMChunk
from lune.tts.router import TTSRouterService
from tests.pipeline.conftest import NATIVE_WINDOW, FakeDetector
from tests.pipeline.harness import FakeSTT, HashEncoder, ScriptedTTSBackend

SESSION_ID = "engine-session"
HEADPHONES = DeviceSnapshot(
    input=DeviceInfo(uid="input", name="Headset", is_builtin=False),
    output=DeviceInfo(uid="output", name="Headphones", is_builtin=False),
)
BUILT_IN = DeviceSnapshot(
    input=HEADPHONES.input,
    output=DeviceInfo(uid="builtin", name="Built-in output", is_builtin=True),
)


def text(value: str) -> Callable[[int, str], ProviderStreamFrame]:
    return lambda generation, attempt: GenerationLLMTextFrame(
        text=value,
        generation_id=generation,
        attempt_id=attempt,
    )


def terminal() -> Callable[[int, str], ProviderStreamFrame]:
    return lambda generation, attempt: ProviderTerminalFrame(
        generation_id=generation,
        attempt_id=attempt,
        status="completed",
    )


class RecordingStreamOwner:
    def __init__(self, snapshot: DeviceSnapshot = HEADPHONES) -> None:
        self.snapshot = snapshot
        self.rebuilt: list[DeviceSnapshot] = []
        self.microphone: list[bool] = []
        self.written: list[PCMChunk] = []
        self.flushes = 0
        self.closed = False

    async def default_devices(self) -> DeviceSnapshot:
        return self.snapshot

    async def rebuild_streams(self, snapshot: DeviceSnapshot) -> None:
        self.rebuilt.append(snapshot)

    async def set_microphone(self, enabled: bool) -> None:
        self.microphone.append(enabled)

    async def write(self, chunk: PCMChunk) -> None:
        self.written.append(chunk)

    async def flush(self) -> None:
        self.flushes += 1

    async def close(self) -> None:
        self.closed = True

    def consume_health(self) -> StreamOwnerHealth:
        return StreamOwnerHealth(input_failed=False, output_failed=False)


def build_engine(tmp_path: Path, owner: RecordingStreamOwner) -> tuple[object, FakeSTT]:
    store = MemoryStore(tmp_path / "private" / "lune.sqlite3")
    store.start_session(SESSION_ID)
    retriever = E5MemoryRetriever(store, HashEncoder())
    stt_holder: list[FakeSTT] = []

    def stt_factory(sink: Callable[[STTEvent], object]) -> FakeSTT:
        stt = FakeSTT(sink)
        stt_holder.append(stt)
        return stt

    terra = ScriptedAttemptProvider(
        "gpt-5.6-terra",
        scripts=((text("你好。"), terminal()),),
    )
    luna = ScriptedAttemptProvider("gpt-5.6-luna", scripts=())
    providers: Mapping[ModelName, ScriptedAttemptProvider] = {
        "gpt-5.6-terra": terra,
        "gpt-5.6-luna": luna,
    }
    backend = ScriptedTTSBackend(chunks=1)
    dependencies = EngineDependencies(
        session_id=SESSION_ID,
        store=store,
        retriever=retriever,
        detector=FakeDetector(),
        stt_factory=stt_factory,  # type: ignore[arg-type]
        providers=providers,
        ledger=BudgetLedger(),
        tts=TTSRouterService(avspeech=backend),
    )
    transport = LocalAudioTransport(max_callbacks=4)
    engine = compose_voice_engine(
        dependencies,
        transport=transport,
        streams=owner,
        input_poll_s=0.001,
        health_poll_s=0.005,
        device_poll_s=0.01,
    )
    return engine, stt_holder[0]


async def wait_until(predicate: Callable[[], bool], *, ticks: int = 500) -> None:
    for _ in range(ticks):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition was not reached")


async def feed(transport: LocalAudioTransport, frames: int, amplitude: int) -> None:
    remaining = frames
    while remaining:
        count = min(NATIVE_WINDOW, remaining)
        pcm = np.full(count, amplitude, dtype="<i2").tobytes()
        assert transport.audio_callback(pcm) is True
        remaining -= count
        await asyncio.sleep(0.002)


@pytest.mark.asyncio
async def test_engine_runs_the_single_pipeline_from_transport_to_recording_output(
    tmp_path: Path,
) -> None:
    owner = RecordingStreamOwner()
    engine, stt = build_engine(tmp_path, owner)

    assert await engine.start() == "mic_off"  # type: ignore[attr-defined]
    assert engine.transport.microphone_enabled is False  # type: ignore[attr-defined]
    assert owner.rebuilt == [HEADPHONES]
    assert await engine.set_microphone(True) == "listening"  # type: ignore[attr-defined]

    await feed(engine.transport, NATIVE_WINDOW * 11, 0)  # type: ignore[attr-defined]
    await feed(engine.transport, NATIVE_WINDOW * 8, 8_000)  # type: ignore[attr-defined]
    await feed(engine.transport, 5_600 + NATIVE_WINDOW, 0)  # type: ignore[attr-defined]
    await wait_until(lambda: len(stt.requests) == 1)
    await stt.emit_final("早安")
    assert await engine.pipeline.session.wait_for_turns() is True  # type: ignore[attr-defined]

    assert len(owner.written) == 1
    assert engine.pipeline.session.reports[-1].outcome == "completed"  # type: ignore[attr-defined]
    assert engine.pipeline.session.state == "listening"  # type: ignore[attr-defined]
    assert engine.background_task_count == 3  # type: ignore[attr-defined]

    await engine.close()  # type: ignore[attr-defined]
    assert owner.closed is True
    assert engine.background_task_count == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_engine_keeps_the_microphone_closed_on_builtin_output_and_after_switch(
    tmp_path: Path,
) -> None:
    owner = RecordingStreamOwner(BUILT_IN)
    engine, _stt = build_engine(tmp_path, owner)
    assert await engine.start() == "paused_unsafe_output"  # type: ignore[attr-defined]
    assert await engine.set_microphone(True) == "paused_unsafe_output"  # type: ignore[attr-defined]
    assert engine.transport.microphone_enabled is False  # type: ignore[attr-defined]

    owner.snapshot = HEADPHONES
    await wait_until(lambda: engine.state == "listening")  # type: ignore[attr-defined]
    assert engine.transport.microphone_enabled is True  # type: ignore[attr-defined]

    owner.snapshot = BUILT_IN
    await wait_until(lambda: engine.state == "paused_unsafe_output")  # type: ignore[attr-defined]
    assert engine.transport.microphone_enabled is False  # type: ignore[attr-defined]
    assert engine.pipeline.coordinator.cancel_events[-1].reason == "device_changed"  # type: ignore[attr-defined]
    await engine.close()  # type: ignore[attr-defined]
