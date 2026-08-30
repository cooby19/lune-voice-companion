"""Compose and own Lune's single local voice pipeline."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from collections.abc import Callable, Coroutine, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, TextIO

from lune.audio.coreaudio import CoreAudioStreamOwner, StreamOwnerHealth
from lune.audio.devices import DeviceSnapshot
from lune.audio.silero import SileroVoiceDetector
from lune.audio.transport import LocalAudioTransport
from lune.config import AppConfig, AudioConfig, PersonaKernel
from lune.diagnostics import SafeDiagnostics
from lune.ipc import UI_COMMAND_NAMES, UI_EVENT_NAMES, CommandRejected, JSONValue, LoopbackIPCServer
from lune.keychain import get_openai_api_key
from lune.llm.budget import BudgetLedger
from lune.llm.contracts import LOCAL_MODEL_NAME, ModelName
from lune.llm.local_qwen import LocalQwenLLMService
from lune.llm.prompt import build_persona_instruction
from lune.llm.provider import LLMProviderFactory, LocalQwenProviderConfig
from lune.llm.streaming import AttemptStreamProvider
from lune.llm.titles import LocalQwenTitleBackend
from lune.memory.embedding import E5MemoryRetriever, LocalE5Encoder
from lune.memory.store import MemoryStore
from lune.memory.summary import RollingSummaryManager
from lune.memory.titles import ThreadTitleBackend, ThreadTitleManager
from lune.memory.usage import persistent_budget_ledger
from lune.paths import LunePaths
from lune.pipeline.coordinator import ProviderFence
from lune.pipeline.factory import STTEventSink, VoicePipeline, build_voice_pipeline
from lune.pipeline.pipecat_provider import PipecatAttemptProvider
from lune.pipeline.playback import DEFAULT_CAPACITY
from lune.pipeline.session import FinalOnlySTT
from lune.pipeline.turn_gate import VoicedDetector
from lune.readiness import AppState, check_readiness
from lune.stt.mlx import build_mlx_stt
from lune.tts.contracts import PCMChunk
from lune.tts.factory import build_tts_router
from lune.tts.router import TTSRouterService
from lune.ui.runtime import EngineControl, EngineFactory, UiCommandError, UiRuntime

# A local WebView keeps up easily, so this only bounds the memory a stalled one
# can cost.  Overflow degrades latency, never accuracy: the reconciling
# snapshot still carries whatever the dropped events would have said.
UI_EVENT_QUEUE_CAPACITY: Final = 256


class AsyncCloser(Protocol):
    async def close(self) -> None: ...


class EngineStreamOwner(Protocol):
    async def default_devices(self) -> DeviceSnapshot: ...

    async def rebuild_streams(self, snapshot: DeviceSnapshot) -> None: ...

    async def set_microphone(self, enabled: bool) -> None: ...

    async def request_microphone_access(self) -> None: ...

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
    primary_model: ModelName | None = None
    max_output_tokens: int = 192
    provider_fences: Sequence[ProviderFence] = ()
    provider_closers: Sequence[AsyncCloser] = ()
    summarizer: RollingSummaryManager | None = None
    titler: ThreadTitleManager | None = None
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
    def session_id(self) -> str | None:
        """The persisted conversation currently bound to the live pipeline."""

        return self._session_id

    @property
    def store(self) -> MemoryStore | None:
        """The local store, exposed only to the authenticated in-process UI host."""

        return self._store

    @property
    def microphone_requested(self) -> bool:
        return self._microphone_requested

    @property
    def degraded_tts(self) -> bool:
        return self.pipeline.session.degraded_tts

    @property
    def output_is_builtin(self) -> bool | None:
        """Expose only the safe built-in-output category to the local UI."""

        return self.pipeline.session.output_is_builtin

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

    async def submit_text(self, text: str, *, speak_text: bool = True) -> AppState:
        if not self._started or self._closed:
            raise RuntimeError("engine is not running")
        return await self.pipeline.session.submit_text(text, speak_text=speak_text)

    async def request_microphone_access(self) -> None:
        """Show the macOS permission prompt without enabling capture."""

        if not self._started or self._closed:
            raise RuntimeError("engine is not running")
        await self.streams.request_microphone_access()

    async def refresh_devices(self) -> AppState:
        """Re-read the current defaults after an explicit UI recheck."""

        if not self._started or self._closed:
            raise RuntimeError("engine is not running")
        async with self._device_lock:
            snapshot = await self.streams.default_devices()
            state = await self.pipeline.session.apply_default_devices(snapshot)
            await self._sync_microphone(state)
        return self.state

    def select_thread(self, thread_id: str) -> None:
        """Select an idle thread without rebuilding the model or audio pipeline."""

        if not self._started or self._closed:
            raise RuntimeError("engine is not running")
        if self._microphone_requested:
            raise RuntimeError("cannot switch conversation while a call is active")
        self.pipeline.session.switch_thread(thread_id)
        self._session_id = thread_id

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
            # Dropped input invalidates the sample timeline, not an answer that
            # is already being generated. Cancelling unconditionally made a slow
            # turn destroy itself on the target Mac: inference starved the input
            # callback, the resulting overflow cancelled the very generation that
            # inference was feeding, and nothing was ever spoken. Speech in
            # progress still cancels, because that audio is the user's next
            # utterance and must not be spliced across the gap.
            if self.pipeline.turn_gate.turn_active:
                await self.pipeline.coordinator.cancel("stream_error")
            else:
                # Same timeline reset the cancel path performs, without moving
                # the fence: the cursor restarts, so the gate cannot splice.
                self.transport.rebuild(generation_id=self.pipeline.coordinator.generation_id)
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
    playback_capacity: int = DEFAULT_CAPACITY,
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
        titler=dependencies.titler,
        rebuild_streams=stream_owner.rebuild_streams,
        primary_model=dependencies.primary_model,
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


@dataclass(frozen=True, slots=True)
class _ProviderComposition:
    """One provider set, already fenced and closeable, for a single composition."""

    providers: Mapping[ModelName, AttemptStreamProvider]
    fences: tuple[ProviderFence, ...]
    closers: tuple[AsyncCloser, ...]
    primary_model: ModelName | None = None
    # Present only where a thread can be named without a request of its own.
    title_backend: ThreadTitleBackend | None = None


def _cloud_composition(
    *,
    api_key: str,
    system_instruction: str,
    max_output_tokens: int,
) -> _ProviderComposition:
    pair = LLMProviderFactory().build_openai_pair(
        api_key=api_key,
        system_instruction=system_instruction,
        max_output_tokens=max_output_tokens,
    )
    terra = PipecatAttemptProvider(model="gpt-5.6-terra", service=pair.primary)
    luna = PipecatAttemptProvider(model="gpt-5.6-luna", service=pair.fallback)
    return _ProviderComposition(
        providers={"gpt-5.6-terra": terra, "gpt-5.6-luna": luna},
        fences=(terra, luna),
        closers=(terra, luna),
    )


async def _local_composition(
    *,
    paths: LunePaths,
    system_instruction: str,
    max_output_tokens: int,
) -> _ProviderComposition:
    """Build the on-device composition and load the weights before the first turn.

    There is no second tier to fall back to, so the primary is pinned: the ledger
    must not try to pick Terra or Luna for a composition that has neither.
    """

    service = LLMProviderFactory().build(
        LocalQwenProviderConfig(
            model_dir=paths.local_llm_dir,
            runtime_python=paths.local_llm_runtime_python,
            system_instruction=system_instruction,
            max_output_tokens=max_output_tokens,
        )
    )
    if not isinstance(service, LocalQwenLLMService):
        raise RuntimeError("the local provider registry returned an unexpected service")
    await service.preload()
    provider = PipecatAttemptProvider(model=LOCAL_MODEL_NAME, service=service)
    return _ProviderComposition(
        providers={LOCAL_MODEL_NAME: provider},
        fences=(provider,),
        # The pipeline worker stops first; the weights are released only after
        # nothing can still ask them for tokens.
        closers=(provider, service),
        primary_model=LOCAL_MODEL_NAME,
        title_backend=LocalQwenTitleBackend(service),
    )


async def build_default_engine(
    paths: LunePaths | None = None,
    *,
    ephemeral_memory: bool = False,
    session_id: str | None = None,
) -> VoiceEngine:
    """Build production adapters; callers must authorize private/hardware use."""

    local_paths = paths or LunePaths.defaults()
    config = AppConfig.load(local_paths.config)
    persona = PersonaKernel.load(local_paths.persona)
    cloud = config.models.provider == "openai_responses"
    api_key = (get_openai_api_key() or "") if cloud else ""
    if cloud and not api_key:
        raise RuntimeError("setup_required")
    local_paths.ensure_private_directories()
    # An ephemeral store keeps a shakedown conversation out of the real
    # transcripts, memories and affinity history, which have no bulk delete.
    store = MemoryStore.ephemeral() if ephemeral_memory else MemoryStore(local_paths.database)
    if session_id is None:
        active_session_id = store.start_session()
    else:
        if store.get_conversation_thread(session_id) is None:
            store.close()
            raise ValueError("unknown conversation thread")
        active_session_id = session_id
    transport = LocalAudioTransport(
        sample_rate=config.audio.sample_rate,
        channels=config.audio.channels,
    )
    streams = CoreAudioStreamOwner(transport)
    composition: _ProviderComposition | None = None
    try:
        retriever = E5MemoryRetriever(store, LocalE5Encoder(local_paths.e5_manifest))
        detector = SileroVoiceDetector(sample_rate=config.audio.sample_rate)
        instruction = build_persona_instruction(persona)
        composition = (
            _cloud_composition(
                api_key=api_key,
                system_instruction=instruction,
                max_output_tokens=config.models.max_output_tokens,
            )
            if cloud
            else await _local_composition(
                paths=local_paths,
                system_instruction=instruction,
                max_output_tokens=config.models.max_output_tokens,
            )
        )

        def stt_factory(emit: STTEventSink) -> FinalOnlySTT:
            return build_mlx_stt(
                manifest_path=local_paths.whisper_manifest,
                generation_id=0,
                emit=emit,
            )

        dependencies = EngineDependencies(
            session_id=active_session_id,
            store=store,
            retriever=retriever,
            detector=detector,
            stt_factory=stt_factory,
            providers=composition.providers,
            ledger=persistent_budget_ledger(store, config.budget),
            tts=build_tts_router(config.tts, local_paths),
            primary_model=composition.primary_model,
            max_output_tokens=config.models.max_output_tokens,
            provider_fences=composition.fences,
            provider_closers=composition.closers,
            titler=(
                None
                if composition.title_backend is None
                else ThreadTitleManager(store, composition.title_backend)
            ),
            audio=config.audio,
        )
        return compose_voice_engine(dependencies, transport=transport, streams=streams)
    except BaseException:
        # A local composition has already spawned its worker by this point, so
        # failing later must not leave the weights resident.
        if composition is not None:
            for closer in composition.closers:
                with suppress(Exception):
                    await closer.close()
        await streams.close()
        store.close()
        raise


async def run(*, microphone: bool = False, ephemeral_memory: bool = False) -> int:
    paths = LunePaths.defaults()
    readiness = check_readiness(paths)
    if readiness.state == "setup_required":
        _report("setup_required", reasons=[reason for reason in readiness.reasons])
        return 2
    engine: VoiceEngine | None = None
    try:
        engine = await build_default_engine(paths, ephemeral_memory=ephemeral_memory)
        _report(await engine.start())
        if microphone:
            # The UI normally sends `set_microphone`; until it exists this flag
            # is the only way to hold a conversation. Cold start stays mic-off.
            _report(await engine.set_microphone(True))
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        await stop.wait()
        return 0
    except Exception as error:
        # The type name only: a message could carry a private path.
        _report("error", reasons=[type(error).__name__])
        return 3
    finally:
        if engine is not None:
            await engine.close()


async def run_ui_ipc(
    paths: LunePaths | None = None,
    *,
    engine_factory: EngineFactory | None = None,
    handoff_stream: TextIO | None = None,
    snapshot_interval_s: float = 2.0,
    install_signal_handlers: bool = True,
) -> int:
    """Run the engine child behind the authenticated local Web UI protocol.

    The one-time handoff is the only stdout output in this mode.  It is meant
    solely for the pywebview parent process; application state and private text
    travel over the authenticated WebSocket instead of a process log.

    Conversation, thread and memory changes reach the UI as incremental events
    the moment they commit.  ``snapshot_interval_s`` only paces the whole-state
    reconciliation that covers what no event carries, so a turn no longer
    re-sends every thread's private text several times a second.
    """

    if snapshot_interval_s <= 0:
        raise ValueError("snapshot interval must be positive")
    local_paths = paths or LunePaths.defaults()
    host_loop = asyncio.get_running_loop()
    host_stop: asyncio.Future[None] = host_loop.create_future()

    def request_host_stop() -> None:
        """Resolve the host-owned shutdown future exactly once.

        The IPC command handler is deliberately decoupled from the supervisor
        task: it only resolves this future, so the ``shutdown`` command and a
        terminating signal both reach the single cleanup block below.
        """

        if not host_stop.done():
            host_stop.set_result(None)

    async def build_engine() -> EngineControl:
        if engine_factory is not None:
            return await engine_factory()
        return await build_default_engine(local_paths)

    events: asyncio.Queue[tuple[str, dict[str, JSONValue]]] = asyncio.Queue(
        maxsize=UI_EVENT_QUEUE_CAPACITY
    )
    overflowed = False

    def publish_ui_event(event: str, payload: dict[str, JSONValue]) -> None:
        """Hand one incremental event to the pump without ever blocking a write.

        The store notifies from the task that committed the row.  Waiting for a
        slow WebView here would stall the pipeline, so a full queue drops the
        event and marks the client as needing the next whole-state snapshot.
        """

        nonlocal overflowed
        try:
            events.put_nowait((event, payload))
        except asyncio.QueueFull:
            overflowed = True

    def drain_overflow() -> bool:
        """Report and clear whether any event was dropped since the last check."""

        nonlocal overflowed
        dropped = overflowed
        overflowed = False
        return dropped

    runtime = UiRuntime(local_paths, build_engine, event_sink=publish_ui_event)
    server: LoopbackIPCServer | None = None

    async def handle_ui_command(command: str, params: Mapping[str, JSONValue]) -> JSONValue:
        try:
            result = await runtime.handle(command, params)
        except UiCommandError as error:
            # Detailed application codes are intentionally not a wire contract:
            # they may reveal whether a private setup artifact exists.
            del error
            raise CommandRejected("command_rejected") from None
        if command == "shutdown":
            # Wake the supervisor without closing the server in this handler:
            # `_dispatch` still owns the final result frame.
            request_host_stop()
        return result

    server = LoopbackIPCServer(
        handle_ui_command,
        command_names=UI_COMMAND_NAMES,
        event_names=UI_EVENT_NAMES,
    )
    startup_task: asyncio.Task[None] | None = None
    broadcaster: asyncio.Task[None] | None = None
    try:
        connection = await server.start()
        _write_ui_handoff(connection.handshake_json(), handoff_stream or sys.stdout)
        # Starting can preload a local model.  Hand the shell its credential
        # first so it can show a responsive, private-data-free preparing state.
        startup_task = asyncio.create_task(runtime.start(), name="lune-ui-startup")
        broadcaster = asyncio.create_task(
            _broadcast_ui_state(server, runtime, events, drain_overflow, snapshot_interval_s),
            name="lune-ui-events",
        )
        if install_signal_handlers:
            _install_ui_signal_handlers(request_host_stop)
        # The command handler schedules this after runtime has accepted the
        # shutdown request.  Signals use the same path, so both closures reach
        # the single cleanup block below.
        await host_stop
        return 0
    except Exception:
        # The desktop parent treats missing/invalid handoff as startup failure.
        # Never print an exception here: paths and model details are private.
        return 3
    finally:
        for task in (broadcaster, startup_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (broadcaster, startup_task) if task is not None),
            return_exceptions=True,
        )
        await server.close()
        await runtime.close()


async def _broadcast_ui_state(
    server: LoopbackIPCServer,
    runtime: UiRuntime,
    events: asyncio.Queue[tuple[str, dict[str, JSONValue]]],
    dropped_any_event: Callable[[], bool],
    reconcile_interval_s: float,
) -> None:
    """Serve incremental events, and reconcile with a whole snapshot rarely.

    Both channels go out from this one task so the authenticated client always
    applies them in the order they were produced.  Sending an event also
    advances the reconciliation baseline: the client has already been told
    about that change, so re-sending every thread's private text would add
    nothing.  Any dropped event clears the baseline instead, which turns the
    next tick into a full correction.

    A frame nobody received advances nothing.  The WebView authenticates after
    the engine has started, so the first snapshot is usually broadcast to an
    empty room; treating it as a baseline left a client that arrived during a
    motionless setup screen with no state at all.

    ``server.broadcast`` drops a peer that cannot keep up rather than waiting on
    it, so a stalled WebView cannot hold this loop or the engine behind it.
    """

    previous: dict[str, JSONValue] | None = None
    loop = asyncio.get_running_loop()
    deadline = loop.time()
    while not runtime.shutdown_requested and server.running:
        timeout = deadline - loop.time()
        if timeout <= 0:
            if dropped_any_event():
                previous = None
            snapshot = runtime.snapshot()
            if snapshot != previous:
                result = await server.broadcast("snapshot", snapshot)
                previous = snapshot if result.delivered else None
            deadline = loop.time() + reconcile_interval_s
            continue
        try:
            event, payload = await asyncio.wait_for(events.get(), timeout=timeout)
        except TimeoutError:
            continue
        result = await server.broadcast(event, payload)
        previous = runtime.snapshot() if result.delivered else None


def _write_ui_handoff(payload: str, stream: TextIO) -> None:
    """Write exactly one compact private handshake line to the inherited pipe."""

    stream.write(payload + "\n")
    stream.flush()


def _install_ui_signal_handlers(request_stop: Callable[[], None]) -> None:
    """Ask the child to close cleanly when its parent is terminated."""

    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(signum, request_stop)


def _report(state: str, *, reasons: Sequence[str] = ()) -> None:
    """Print bounded state, never transcripts, prompts or private paths."""

    line = state if not reasons else f"{state}: {', '.join(reasons)}"
    print(line, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lune-engine")
    parser.add_argument(
        "--microphone",
        action="store_true",
        help="open the microphone once the engine is listening",
    )
    parser.add_argument(
        "--ephemeral-memory",
        action="store_true",
        help="keep this session out of the private database",
    )
    parser.add_argument(
        "--ui-ipc",
        action="store_true",
        help="serve the authenticated local Web UI child protocol",
    )
    args = parser.parse_args(argv)
    if args.ui_ipc:
        if args.microphone or args.ephemeral_memory:
            parser.error("--ui-ipc cannot be combined with microphone or ephemeral-memory")
        return asyncio.run(run_ui_ipc())
    return asyncio.run(run(microphone=args.microphone, ephemeral_memory=args.ephemeral_memory))


if __name__ == "__main__":
    raise SystemExit(main())
