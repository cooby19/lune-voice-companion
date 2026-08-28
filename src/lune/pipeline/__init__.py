"""The assembled voice pipeline with one central generation fence."""

from lune.pipeline.benchmark import (
    EndToEndGate,
    InterruptionGate,
    InterruptionSample,
    TurnLatencySample,
    collect_interruption_samples,
    collect_latency_samples,
    evaluate_end_to_end,
    evaluate_interruption,
)
from lune.pipeline.contracts import (
    CancelEvent,
    CancelReason,
    TurnGateEvent,
    TurnStarted,
    UtteranceCaptured,
)
from lune.pipeline.coordinator import GenerationCoordinator
from lune.pipeline.enricher import ContextEnricher
from lune.pipeline.factory import VoicePipeline, build_voice_pipeline
from lune.pipeline.pipecat_provider import PipecatAttemptProvider
from lune.pipeline.playback import AudioOutputDevice, PlaybackSink
from lune.pipeline.session import TurnReport, VoiceSession
from lune.pipeline.turn_gate import VoiceTurnGate

__all__ = [
    "AudioOutputDevice",
    "CancelEvent",
    "CancelReason",
    "ContextEnricher",
    "EndToEndGate",
    "GenerationCoordinator",
    "InterruptionGate",
    "InterruptionSample",
    "PipecatAttemptProvider",
    "PlaybackSink",
    "TurnGateEvent",
    "TurnLatencySample",
    "TurnReport",
    "TurnStarted",
    "UtteranceCaptured",
    "VoicePipeline",
    "VoiceSession",
    "VoiceTurnGate",
    "build_voice_pipeline",
    "collect_interruption_samples",
    "collect_latency_samples",
    "evaluate_end_to_end",
    "evaluate_interruption",
]
