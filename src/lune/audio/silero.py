"""Silero acoustic classifier without Pipecat's quantized turn timing."""

from __future__ import annotations

from typing import Protocol, cast

import numpy as np

from lune.audio.types import AudioSpan


class ConfidenceAnalyzer(Protocol):
    """Small seam used to test the bundled Silero analyzer deterministically."""

    def set_sample_rate(self, sample_rate: int) -> None: ...

    def num_frames_required(self) -> int: ...

    def voice_confidence(self, buffer: bytes) -> object: ...


class SileroVoiceDetector:
    """Map one native Silero window to voiced/unvoiced.

    Pipecat's analyzer deliberately supplies acoustic confidence only. Lune's
    sample-count policy owns the 100/300/350 ms timing so 32 ms Silero windows
    cannot silently round those product thresholds.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        confidence_threshold: float = 0.7,
        analyzer: ConfidenceAnalyzer | None = None,
    ) -> None:
        if sample_rate not in (8_000, 16_000):
            raise ValueError("Silero supports only 8 kHz or 16 kHz audio")
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError("confidence threshold must be between zero and one")
        if analyzer is None:
            from pipecat.audio.vad.silero import SileroVADAnalyzer

            analyzer = cast(ConfidenceAnalyzer, SileroVADAnalyzer(sample_rate=sample_rate))
        analyzer.set_sample_rate(sample_rate)
        self.sample_rate = sample_rate
        self.confidence_threshold = confidence_threshold
        self._analyzer = analyzer

    @property
    def frames_required(self) -> int:
        return self._analyzer.num_frames_required()

    def voice_confidence(self, span: AudioSpan) -> float:
        if span.sample_rate != self.sample_rate or span.channels != 1:
            raise ValueError("Silero input must be mono at the configured sample rate")
        if span.frame_count != self.frames_required:
            raise ValueError("Silero input must contain exactly one native analysis window")
        raw_confidence = np.asarray(self._analyzer.voice_confidence(span.pcm))
        if raw_confidence.size != 1:
            raise RuntimeError("Silero returned an invalid confidence shape")
        confidence = float(raw_confidence.item())
        return min(1.0, max(0.0, confidence))

    def is_voiced(self, span: AudioSpan) -> bool:
        return self.voice_confidence(span) >= self.confidence_threshold
