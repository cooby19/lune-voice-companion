"""Typed M6 contracts for central cancellation and turn assembly."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from lune.audio.types import AudioSpan

type CancelReason = Literal[
    "barge_in",
    "device_changed",
    "stt_timeout",
    "output_overflow",
    "shutdown",
    "error",
]
type CancelStage = Literal[
    "playback",
    "tts",
    "stt",
    "provider",
    "proposals",
    "turn_gate",
    "transport",
]
type TurnOutcome = Literal[
    "completed",
    "cancelled",
    "error",
    "budget_locked",
    "setup_required",
]


@dataclass(frozen=True, slots=True)
class CancelEvent:
    """One completed pass through the single cancellation entry point."""

    previous_generation_id: int
    generation_id: int
    reason: CancelReason
    audible_stop_ms: float = 0.0
    failed_stages: tuple[CancelStage, ...] = ()

    def __post_init__(self) -> None:
        if self.previous_generation_id < 0 or self.generation_id <= self.previous_generation_id:
            raise ValueError("cancelling must advance the generation ID")
        if self.audible_stop_ms < 0.0:
            raise ValueError("audible stop duration cannot be negative")

    @property
    def clean(self) -> bool:
        return not self.failed_stages


@dataclass(frozen=True, slots=True)
class TurnStarted:
    """Confirmed speech onset; ``barge_in`` means Lune was already audible."""

    generation_id: int
    at_sample: int
    voice_onset_sample: int
    barge_in: bool

    def __post_init__(self) -> None:
        if self.generation_id < 0:
            raise ValueError("generation ID cannot be negative")
        if not 0 <= self.voice_onset_sample <= self.at_sample:
            raise ValueError("voice onset cannot follow its confirmation")


@dataclass(frozen=True, slots=True)
class UtteranceCaptured:
    """The complete utterance, pre-roll included, ready for final-only STT.

    ``last_voiced_sample`` is the end-to-end clock's start: the gate's own
    boundary excludes the end-of-turn silence that follows it.
    """

    generation_id: int
    audio: AudioSpan = field(repr=False)
    last_voiced_sample: int = 0
    pre_roll_truncated: bool = False
    max_length_reached: bool = False

    def __post_init__(self) -> None:
        if self.audio.generation_id != self.generation_id:
            raise ValueError("captured audio must carry the capturing generation")
        if self.audio.frame_count == 0:
            raise ValueError("a captured utterance cannot be empty")
        if not self.audio.start_sample <= self.last_voiced_sample <= self.audio.end_sample:
            raise ValueError("the last voiced sample must fall inside the utterance")

    @property
    def trailing_silence_frames(self) -> int:
        return self.audio.end_sample - self.last_voiced_sample


type TurnGateEvent = TurnStarted | UtteranceCaptured
