"""Assemble the one fixed M6 path so no caller can wire it a different way."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from lune.audio.devices import DeviceSnapshot, MaybeAwaitable
from lune.audio.preroll import PreRollBuffer
from lune.audio.transport import LocalAudioTransport
from lune.audio.vad import TurnPolicy, TurnPolicyConfig
from lune.config import AudioConfig
from lune.diagnostics import SafeDiagnostics
from lune.llm.budget import BudgetLedger
from lune.llm.contracts import ModelName
from lune.llm.streaming import AttemptStreamProvider
from lune.memory.embedding import E5MemoryRetriever
from lune.memory.proposals import ProposalHost
from lune.memory.store import MemoryStore
from lune.memory.summary import RollingSummaryManager
from lune.pipeline.coordinator import GenerationCoordinator, ProviderFence
from lune.pipeline.enricher import ContextEnricher
from lune.pipeline.playback import DEFAULT_CAPACITY, AudioOutputDevice, PlaybackSink
from lune.pipeline.session import FinalOnlySTT, ProviderFenceGroup, SampleClock, VoiceSession
from lune.pipeline.turn_gate import VoicedDetector, VoiceTurnGate
from lune.stt.contracts import STTEvent
from lune.tts.router import TTSRouterService

type STTEventSink = Callable[[STTEvent], Awaitable[None]]

CONFIRMATION_MARGIN_MS = 50


class DeferredSTTSink:
    """Break the cycle: STT is built before the session that consumes its events."""

    def __init__(self) -> None:
        self._target: STTEventSink | None = None

    def bind(self, target: STTEventSink) -> None:
        if self._target is not None:
            raise RuntimeError("the STT sink is already bound")
        self._target = target

    async def __call__(self, event: STTEvent) -> None:
        if self._target is None:
            raise RuntimeError("the STT sink has no session bound")
        await self._target(event)


@dataclass(frozen=True, slots=True)
class VoicePipeline:
    coordinator: GenerationCoordinator
    turn_gate: VoiceTurnGate
    playback: PlaybackSink
    session: VoiceSession
    stt: FinalOnlySTT
    proposals: ProposalHost


def build_voice_pipeline(
    *,
    session_id: str,
    store: MemoryStore,
    retriever: E5MemoryRetriever,
    detector: VoicedDetector,
    stt_factory: Callable[[STTEventSink], FinalOnlySTT],
    providers: Mapping[ModelName, AttemptStreamProvider],
    ledger: BudgetLedger,
    tts: TTSRouterService,
    output_device: AudioOutputDevice,
    provider_fences: Sequence[ProviderFence] = (),
    summarizer: RollingSummaryManager | None = None,
    rebuild_streams: Callable[[DeviceSnapshot], MaybeAwaitable] | None = None,
    primary_model: ModelName | None = None,
    transport: LocalAudioTransport | None = None,
    sample_clock: SampleClock | None = None,
    audio: AudioConfig | None = None,
    diagnostics: SafeDiagnostics | None = None,
    playback_capacity: int = DEFAULT_CAPACITY,
    max_output_tokens: int = 192,
    stt_timeout_s: float = 10.0,
) -> VoicePipeline:
    """Build transport → gate → STT → context → provider → gate → TTS → output."""

    settings = audio or AudioConfig()
    turn_gate = VoiceTurnGate(
        detector=detector,
        policy=TurnPolicy(
            TurnPolicyConfig(
                sample_rate=settings.sample_rate,
                idle_start_ms=settings.turn_start_ms,
                barge_in_ms=settings.barge_in_ms,
                end_silence_ms=settings.end_silence_ms,
            )
        ),
        pre_roll=PreRollBuffer(
            sample_rate=settings.sample_rate,
            channels=settings.channels,
            capacity_ms=settings.pre_roll_ms + settings.barge_in_ms + CONFIRMATION_MARGIN_MS,
            required_pre_roll_ms=settings.pre_roll_ms,
            max_confirmation_ms=settings.barge_in_ms,
        ),
        sample_rate=settings.sample_rate,
        channels=settings.channels,
    )
    playback = PlaybackSink(output_device, capacity=playback_capacity)
    proposals = ProposalHost(store, retriever)
    sink = DeferredSTTSink()
    stt = stt_factory(sink)
    coordinator = GenerationCoordinator(
        playback=playback,
        tts=tts,
        stt=stt,
        turn_gate=turn_gate,
        proposals=proposals,
        provider=ProviderFenceGroup(provider_fences) if provider_fences else None,
        transport=transport,
        diagnostics=diagnostics,
    )
    session = VoiceSession(
        session_id=session_id,
        store=store,
        coordinator=coordinator,
        turn_gate=turn_gate,
        stt=stt,
        enricher=ContextEnricher(store, retriever),
        providers=providers,
        ledger=ledger,
        tts=tts,
        playback=playback,
        proposals=proposals,
        summarizer=summarizer,
        rebuild_streams=rebuild_streams,
        primary_model=primary_model,
        max_output_tokens=max_output_tokens,
        stt_timeout_s=stt_timeout_s,
        sample_clock=sample_clock or transport,
        diagnostics=diagnostics,
    )
    sink.bind(session.on_stt_event)
    return VoicePipeline(
        coordinator=coordinator,
        turn_gate=turn_gate,
        playback=playback,
        session=session,
        stt=stt,
        proposals=proposals,
    )
