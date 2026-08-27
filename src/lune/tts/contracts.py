"""Privacy-safe streaming TTS contracts shared by every local backend."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol

type TTSLanguageHint = Literal["zh", "en", "auto"]
type TTSFailureCode = Literal[
    "setup_required",
    "backend_unavailable",
    "protocol_error",
    "synthesis_failed",
    "cancelled",
]


class TTSBackendError(RuntimeError):
    """Finite, content-free backend error safe to surface in diagnostics."""

    def __init__(self, code: TTSFailureCode) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class TTSRequest:
    request_id: str
    generation_id: int
    text: str = field(repr=False)
    language_hint: TTSLanguageHint | None = None

    def __post_init__(self) -> None:
        if not self.request_id or len(self.request_id) > 128:
            raise ValueError("request ID must contain 1 to 128 characters")
        if not 0 <= self.generation_id < 2**63:
            raise ValueError("generation ID is outside the supported range")
        if not self.text.strip() or len(self.text) > 8_000:
            raise ValueError("text must contain 1 to 8,000 non-whitespace characters")


@dataclass(frozen=True, slots=True)
class PCMChunk:
    generation_id: int
    sample_rate: int
    channels: int
    data: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not 0 <= self.generation_id < 2**63:
            raise ValueError("generation ID is outside the supported range")
        if not 8_000 <= self.sample_rate <= 192_000:
            raise ValueError("sample rate is outside the supported PCM range")
        if not 1 <= self.channels <= 8:
            raise ValueError("channel count is outside the supported PCM range")
        if not self.data or len(self.data) % (2 * self.channels):
            raise ValueError("PCM must be non-empty interleaved signed 16-bit audio")


class StreamingTTSBackend(Protocol):
    def synthesize(self, request: TTSRequest) -> AsyncIterator[PCMChunk]: ...

    async def cancel(self, generation_id: int) -> None: ...

    async def close(self) -> None: ...
