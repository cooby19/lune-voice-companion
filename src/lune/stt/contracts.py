"""Privacy-safe contracts for final-only speech recognition."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from lune.audio.types import AudioSpan

type LanguageHint = Literal["zh", "en"]
type STTFailureCode = Literal["setup_required", "inference_failed"]


@dataclass(frozen=True, slots=True)
class TranscriptionRequest:
    request_id: str
    generation_id: int
    audio: AudioSpan = field(repr=False)
    language_hint: LanguageHint | None = None

    def __post_init__(self) -> None:
        if not self.request_id or len(self.request_id) > 128:
            raise ValueError("request ID must contain 1 to 128 characters")
        if self.generation_id < 0 or self.audio.generation_id != self.generation_id:
            raise ValueError("request and audio generation IDs must match")
        if self.audio.sample_rate != 16_000 or self.audio.channels != 1:
            raise ValueError("STT requires 16 kHz mono PCM")
        if self.audio.frame_count == 0:
            raise ValueError("STT requires a non-empty audio span")


@dataclass(frozen=True, slots=True)
class FinalTranscript:
    """The only transcript event exposed by M2; interim output has no public type."""

    request_id: str
    generation_id: int
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class STTFailure:
    request_id: str
    generation_id: int
    code: STTFailureCode


type STTEvent = FinalTranscript | STTFailure
