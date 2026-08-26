from __future__ import annotations

from dataclasses import dataclass

import pytest

from lune.audio.silero import SileroVoiceDetector
from lune.audio.types import AudioSpan


@dataclass
class FakeAnalyzer:
    confidence: float
    frames: int = 512

    def set_sample_rate(self, sample_rate: int) -> None:
        assert sample_rate in (8_000, 16_000)

    def num_frames_required(self) -> int:
        return self.frames

    def voice_confidence(self, buffer: bytes) -> float:
        assert len(buffer) == self.frames * 2
        return self.confidence


def silence(*, frames: int = 512, sample_rate: int = 16_000) -> AudioSpan:
    return AudioSpan(
        pcm=b"\x00\x00" * frames,
        start_sample=0,
        end_sample=frames,
        generation_id=1,
        sample_rate=sample_rate,
    )


def test_detector_uses_silero_confidence_without_turn_timing() -> None:
    detector = SileroVoiceDetector(analyzer=FakeAnalyzer(0.71))
    assert detector.frames_required == 512
    assert detector.is_voiced(silence())


def test_detector_requires_one_native_window() -> None:
    detector = SileroVoiceDetector(analyzer=FakeAnalyzer(0.2))
    with pytest.raises(ValueError):
        detector.is_voiced(silence(frames=511))


def test_bundled_silero_backend_analyzes_silence_without_download() -> None:
    detector = SileroVoiceDetector()
    assert detector.frames_required == 512
    assert not detector.is_voiced(silence())
