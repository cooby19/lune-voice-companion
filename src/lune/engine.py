"""Compose and own Lune's single local voice pipeline."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable, Coroutine, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol

from lune.audio.coreaudio import CoreAudioStreamOwner, StreamOwnerHealth
from lune.audio.devices import DeviceSnapshot
from lune.audio.silero import SileroVoiceDetector
from lune.audio.transport import LocalAudioTransport
from lune.config import AppConfig, AudioConfig, PersonaKernel
from lune.diagnostics import SafeDiagnostics
from lune.keychain import get_openai_api_key
from lune.llm.budget import BudgetLedger
from lune.llm.contracts import ModelName
from lune.llm.prompt import build_persona_instruction
from lune.llm.provider import LLMProviderFactory
from lune.llm.streaming import AttemptStreamProvider
from lune.memory.embedding import E5MemoryRetriever, LocalE5Encoder
from lune.memory.store import MemoryStore
from lune.memory.summary import RollingSummaryManager
from lune.memory.usage import persistent_budget_ledger
from lune.paths import LunePaths
from lune.pipeline.coordinator import ProviderFence
from lune.pipeline.factory import STTEventSink, VoicePipeline, build_voice_pipeline
from lune.pipeline.pipecat_provider import PipecatAttemptProvider
from lune.pipeline.session import FinalOnlySTT
from lune.pipeline.turn_gate import VoicedDetector
from lune.readiness import AppState, check_readiness
from lune.stt.mlx import build_mlx_stt
from lune.tts.contracts import PCMChunk
from lune.tts.factory import build_tts_router
from lune.tts.router import TTSRouterService


class AsyncCloser(Protocol):
    async def close(self) -> None: ...


class EngineStreamOwner(Protocol):
    async def default_devices(self) -> DeviceSnapshot: ...

    async def rebuild_streams(self, snapshot: DeviceSnapshot) -> None: ...

    async def set_microphone(self, enabled: bool) -> None: ...

    async def write(self, chunk: PCMChunk) -> None: ...

    async def flush(self) -> None: ...

    async def close(self) -> None: ...

    def consume_health(self) -> StreamOwnerHealth: ...


@dataclass(frozen=True, slots=True)
class EngineDependencies:
    """All collaborators needed by the one allowed pipeline composition."""

    session_id: str
    store: MemoryStore
    retriever: E5MemoryRetriever
    detector: VoicedDetector
    stt_factory: Callable[[STTEventSink], FinalOnlySTT]
    providers: Mapping[ModelName, AttemptStreamProvider]
    ledger: BudgetLedger
    tts: TTSRouterService
    max_output_tokens: int = 192
    provider_fences: Sequence[ProviderFence] = ()
    provider_closers: Sequence[AsyncCloser] = ()
    summarizer: RollingSummaryManager | None = None
    audio: AudioConfig = field(default_factory=AudioConfig)
    diagnostics: SafeDiagnostics | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.max_output_tokens <= 192:
            raise ValueError("M3 output limit must be between one and 192 tokens")


class VoiceEngine:
    """Drive devices and PCM around the fixed pipeline without rewiring it."""

    def __init__(
        self,
        *,
        pipeline: VoicePipeline,
        transport: LocalAudioTransport,
        streams: EngineStreamOwner,
        provider_closers: Sequence[AsyncCloser] = (),
        store: MemoryStore | None = None,
        session_id: str | None = None,
        input_poll_s: float = 0.002,
        health_poll_s: float = 0.01,
        device_poll_s: float = 0.25,
    ) -> None:
        if min(input_poll_s, health_poll_s, device_poll_s) <= 0:
            raise ValueError("engine polling intervals must be positive")
        self.pipeline = pipeline
        self.transport = transport
        self.streams = streams
        self._provider_closers = tuple(provider_closers)
        self._store = store
        self._session_id = session_id
        self._input_poll_s = input_poll_s
        self._health_poll_s = health_poll_s
        self._device_poll_s = device_poll_s
        self._tasks: set[asyncio.Task[None]] = set()
        self._device_lock = asyncio.Lock()
        self._microphone_requested = False
        self._started = False
        self._closed = False

    @property
    def state(self) -> AppState:
        return self.pipeline.session.state

    @property
    def background_task_count(self) -> int:
        return sum(not task.done() for task in self._tasks)

    async def start(self) -> AppState:
        if self._closed:
            raise RuntimeError("engine is closed")
        if self._started:
            return self.state
        await self.pipeline.session.start()
        snapshot = await self.streams.default_devices()
        await self.pipeline.session.apply_default_devices(snapshot)
        self.transport.set_microphone(False)
        await self.streams.set_microphone(False)
        self._started = True
        self._spawn(self._pump_input(), "lune-engine-input")
        self._spawn(self._watch_health(), "lune-engine-health")
        self._spawn(self._watch_devices(), "lune-engine-devices")
        return self.state

    async def set_microphone(self, enabled: bool) -> AppState:
        if not self._started or self._closed:
            raise RuntimeError("engine is not running")
        if not enabled and self._microphone_requested:
            await self.pipeline.coordinator.cancel("microphone_off")
        self._microphone_requested = enabled
        state = self.pipeline.session.set_microphone(enabled)
        try:
            await self._sync_microphone(state)
        except Exception:
            self._microphone_requested = False
            self.transport.set_microphone(False)
            self.pipeline.session.set_microphone(False)
            await self.pipeline.coordinator.cancel("stream_error")
            raise
        return self.state

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._started:
            await self.pipeline.coordinator.cancel("shutdown")
        self._microphone_requested = False
        self.transport.set_microphone(False)
        with suppress(Exception):
            await self.streams.set_microphone(False)
        for task in tuple(self._tasks):
            task.cancel()
        for task in tuple(self._tasks):
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        with suppress(Exception):
            await self.pipeline.session.close()
        # Playback normally owns stream closure; retain this fallback for a
        # partial session shutdown so PortAudio resources cannot survive.
        with suppress(Exception):
            await self.streams.close()
        seen: set[int] = set()
        for closer in self._provider_closers:
            if id(closer) in seen:
                continue
            seen.add(id(closer))
            with suppress(Exception):
                await closer.close()
        if self._store is not None:
            if self._session_id is not None:
                try:
                    self._store.end_session(self._session_id)
                except ValueError:
                    pass
            self._store.close()

    def _spawn(self, coroutine: Coroutine[Any, Any, None], name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _sync_microphone(self, state: AppState | None = None) -> None:
        actual = self._microphone_requested and (state or self.state) == "listening"
        if not actual:
            await self.streams.set_microphone(False)
            self.transport.set_microphone(False)
            return
        # Open first while LocalAudioTransport is still off. Any eager callback
        # is copied then discarded, preserving mic-off until setup succeeds.
        await self.streams.set_microphone(True)
        self.transport.set_microphone(True)

    async def _pump_input(self) -> None:
        while True:
            handled = False
            while (span := self.transport.read_nowait()) is not None:
                handled = True
                try:
                    await self.pipeline.session.handle_audio(span)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await self._recover_streams()
                    break
            if not handled:
                await asyncio.sleep(self._input_poll_s)

    async def _watch_health(self) -> None:
        while True:
            await asyncio.sleep(self._health_poll_s)
            stream_health = self.streams.consume_health()
            if (
                self.transport.health().overflowed
                or stream_health.input_failed
                or stream_health.output_failed
            ):
                await self._recover_streams()

    async def _watch_devices(self) -> None:
        while True:
            await asyncio.sleep(self._device_poll_s)
            try:
                snapshot = await self.streams.default_devices()
            except Exception:
                snapshot = None
            if snapshot is None:
                continue
            async with self._device_lock:
                try:
                    state = await self.pipeline.session.apply_default_devices(snapshot)
                    await self._sync_microphone(state)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._microphone_requested = False
                    self.transport.set_microphone(False)
                    self.pipeline.session.set_microphone(False)
                    with suppress(Exception):
                        await self.streams.set_microphone(False)

    async def _recover_streams(self) -> None:
        async with self._device_lock:
            await self.pipeline.coordinator.cancel("stream_error")
            try:
                snapshot = await self.streams.default_devices()
                await self.streams.rebuild_streams(snapshot)
                await self.pipeline.session.apply_default_devices(snapshot)
                await self._sync_microphone()
            except Exception:
                self._microphone_requested = False
                self.transport.set_microphone(False)
                self.pipeline.session.set_microphone(False)


def compose_voice_engine(
    dependencies: EngineDependencies,
    *,
    transport: LocalAudioTransport | None = None,
    streams: EngineStreamOwner | None = None,
    playback_capacity: int = 32,
    stt_timeout_s: float = 10.0,
    input_poll_s: float = 0.002,
    health_poll_s: float = 0.01,
    device_poll_s: float = 0.25,
) -> VoiceEngine:
    """Use ``build_voice_pipeline`` as the sole engine composition point."""

    local_transport = transport or LocalAudioTransport(
        sample_rate=dependencies.audio.sample_rate,
        channels=dependencies.audio.channels,
    )
    stream_owner = streams or CoreAudioStreamOwner(local_transport)
    pipeline = build_voice_pipeline(
        session_id=dependencies.session_id,
        store=dependencies.store,
        retriever=dependencies.retriever,
        detector=dependencies.detector,
        stt_factory=dependencies.stt_factory,
        providers=dependencies.providers,
        ledger=dependencies.ledger,
        tts=dependencies.tts,
        output_device=stream_owner,
        provider_fences=dependencies.provider_fences,
        summarizer=dependencies.summarizer,
        rebuild_streams=stream_owner.rebuild_streams,
        transport=local_transport,
        audio=dependencies.audio,
        diagnostics=dependencies.diagnostics,
        playback_capacity=playback_capacity,
        max_output_tokens=dependencies.max_output_tokens,
        stt_timeout_s=stt_timeout_s,
    )
    return VoiceEngine(
        pipeline=pipeline,
        transport=local_transport,
        streams=stream_owner,
        provider_closers=dependencies.provider_closers,
        store=dependencies.store,
        session_id=dependencies.session_id,
        input_poll_s=input_poll_s,
        health_poll_s=health_poll_s,
        device_poll_s=device_poll_s,
    )


async def build_default_engine(paths: LunePaths | None = None) -> VoiceEngine:
    """Build production adapters; callers must authorize private/hardware use."""

    local_paths = paths or LunePaths.defaults()
    config = AppConfig.load(local_paths.config)
    persona = PersonaKernel.load(local_paths.persona)
    api_key = get_openai_api_key()
    if not api_key:
        raise RuntimeError("setup_required")
    local_paths.ensure_private_directories()
    store = MemoryStore(local_paths.database)
    session_id = store.start_session()
    transport = LocalAudioTransport(
        sample_rate=config.audio.sample_rate,
        channels=config.audio.channels,
    )
    streams = CoreAudioStreamOwner(transport)
    try:
        retriever = E5MemoryRetriever(store, LocalE5Encoder(local_paths.e5_manifest))
        detector = SileroVoiceDetector(sample_rate=config.audio.sample_rate)
        provider_factory = LLMProviderFactory()
        pair = provider_factory.build_openai_pair(
            api_key=api_key,
            system_instruction=build_persona_instruction(persona),
            max_output_tokens=config.models.max_output_tokens,
        )
        terra = PipecatAttemptProvider(model="gpt-5.6-terra", service=pair.primary)
        luna = PipecatAttemptProvider(model="gpt-5.6-luna", service=pair.fallback)
        providers: dict[ModelName, PipecatAttemptProvider] = {
            "gpt-5.6-terra": terra,
            "gpt-5.6-luna": luna,
        }

        def stt_factory(emit: STTEventSink) -> FinalOnlySTT:
            return build_mlx_stt(
                manifest_path=local_paths.whisper_manifest,
                generation_id=0,
                emit=emit,
            )

        dependencies = EngineDependencies(
            session_id=session_id,
            store=store,
            retriever=retriever,
            detector=detector,
            stt_factory=stt_factory,
            providers=providers,
            ledger=persistent_budget_ledger(store, config.budget),
            tts=build_tts_router(config.tts, local_paths),
            max_output_tokens=config.models.max_output_tokens,
            provider_fences=(terra, luna),
            provider_closers=(terra, luna),
            audio=config.audio,
        )
        return compose_voice_engine(dependencies, transport=transport, streams=streams)
    except BaseException:
        await streams.close()
        store.close()
        raise


async def run() -> int:
    paths = LunePaths.defaults()
    readiness = check_readiness(paths)
    if readiness.state == "setup_required":
        return 2
    engine: VoiceEngine | None = None
    try:
        engine = await build_default_engine(paths)
        await engine.start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        await stop.wait()
        return 0
    except Exception:
        return 3
    finally:
        if engine is not None:
            await engine.close()


def main() -> int:
    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
