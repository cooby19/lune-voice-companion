"""Canonical signed-16-bit PCM spans addressed by absolute sample offsets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

BYTES_PER_SAMPLE: Final[int] = 2


def milliseconds_to_samples(milliseconds: int, sample_rate: int) -> int:
    if milliseconds < 0 or sample_rate <= 0:
        raise ValueError("milliseconds and sample rate must be non-negative")
    numerator = milliseconds * sample_rate
    if numerator % 1_000:
        raise ValueError("threshold must map to an exact number of samples")
    return numerator // 1_000


@dataclass(frozen=True, slots=True)
class AudioSpan:
    """A contiguous PCM region using [start_sample, end_sample) coordinates."""

    pcm: bytes
    start_sample: int
    end_sample: int
    generation_id: int
    sample_rate: int = 16_000
    channels: int = 1

    def __post_init__(self) -> None:
        if self.start_sample < 0 or self.end_sample < self.start_sample:
            raise ValueError("invalid sample range")
        if self.generation_id < 0:
            raise ValueError("generation ID cannot be negative")
        if self.sample_rate <= 0 or self.channels <= 0:
            raise ValueError("invalid PCM format")
        expected_bytes = self.frame_count * self.channels * BYTES_PER_SAMPLE
        if len(self.pcm) != expected_bytes:
            raise ValueError("PCM byte length does not match sample range")

    @property
    def frame_count(self) -> int:
        return self.end_sample - self.start_sample

    @property
    def bytes_per_frame(self) -> int:
        return self.channels * BYTES_PER_SAMPLE

    def trim_left(self, start_sample: int) -> AudioSpan:
        if not self.start_sample <= start_sample <= self.end_sample:
            raise ValueError("trim point is outside span")
        byte_offset = (start_sample - self.start_sample) * self.bytes_per_frame
        return AudioSpan(
            pcm=self.pcm[byte_offset:],
            start_sample=start_sample,
            end_sample=self.end_sample,
            generation_id=self.generation_id,
            sample_rate=self.sample_rate,
            channels=self.channels,
        )

    def slice(self, start_sample: int, end_sample: int) -> AudioSpan:
        if not self.start_sample <= start_sample <= end_sample <= self.end_sample:
            raise ValueError("slice is outside span")
        start_byte = (start_sample - self.start_sample) * self.bytes_per_frame
        end_byte = (end_sample - self.start_sample) * self.bytes_per_frame
        return AudioSpan(
            pcm=self.pcm[start_byte:end_byte],
            start_sample=start_sample,
            end_sample=end_sample,
            generation_id=self.generation_id,
            sample_rate=self.sample_rate,
            channels=self.channels,
        )
