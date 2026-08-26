"""Bounded, absolute-offset PCM ring used to recover barge-in speech onset."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from lune.audio.types import AudioSpan, milliseconds_to_samples


@dataclass(frozen=True, slots=True)
class PreRollCapture:
    audio: AudioSpan
    requested_start_sample: int
    pre_roll_truncated: bool


class PreRollBuffer:
    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        channels: int = 1,
        capacity_ms: int = 700,
        required_pre_roll_ms: int = 350,
        max_confirmation_ms: int = 300,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.pre_roll_samples = milliseconds_to_samples(required_pre_roll_ms, sample_rate)
        self.capacity_samples = milliseconds_to_samples(capacity_ms, sample_rate)
        minimum = self.pre_roll_samples + milliseconds_to_samples(max_confirmation_ms, sample_rate)
        if self.capacity_samples < minimum:
            raise ValueError("pre-roll capacity must include the confirmation window")
        self._spans: deque[AudioSpan] = deque()
        self._generation_id: int | None = None

    @property
    def start_sample(self) -> int | None:
        return self._spans[0].start_sample if self._spans else None

    @property
    def end_sample(self) -> int | None:
        return self._spans[-1].end_sample if self._spans else None

    def clear(self) -> None:
        self._spans.clear()
        self._generation_id = None

    def append(self, span: AudioSpan) -> None:
        if span.sample_rate != self.sample_rate or span.channels != self.channels:
            raise ValueError("PCM format changed without rebuilding pre-roll")
        if span.frame_count == 0:
            return
        if self._generation_id is not None and span.generation_id != self._generation_id:
            self.clear()
        if self._spans and span.start_sample != self._spans[-1].end_sample:
            raise ValueError("pre-roll spans must be contiguous")
        self._generation_id = span.generation_id
        self._spans.append(span)
        self._trim_to_capacity()

    def _trim_to_capacity(self) -> None:
        end = self.end_sample
        if end is None:
            return
        keep_from = max(0, end - self.capacity_samples)
        while self._spans and self._spans[0].end_sample <= keep_from:
            self._spans.popleft()
        if self._spans and self._spans[0].start_sample < keep_from:
            self._spans[0] = self._spans[0].trim_left(keep_from)

    def capture(self, *, voice_onset_sample: int, confirmation_sample: int) -> PreRollCapture:
        if not self._spans or self._generation_id is None:
            raise RuntimeError("pre-roll buffer is empty")
        available_start = self._spans[0].start_sample
        available_end = self._spans[-1].end_sample
        if not available_start <= voice_onset_sample <= confirmation_sample <= available_end:
            raise ValueError("requested capture is outside buffered audio")
        requested_start = max(0, voice_onset_sample - self.pre_roll_samples)
        actual_start = max(requested_start, available_start)
        pieces: list[bytes] = []
        for span in self._spans:
            overlap_start = max(actual_start, span.start_sample)
            overlap_end = min(confirmation_sample, span.end_sample)
            if overlap_start < overlap_end:
                pieces.append(span.slice(overlap_start, overlap_end).pcm)
        audio = AudioSpan(
            pcm=b"".join(pieces),
            start_sample=actual_start,
            end_sample=confirmation_sample,
            generation_id=self._generation_id,
            sample_rate=self.sample_rate,
            channels=self.channels,
        )
        return PreRollCapture(
            audio=audio,
            requested_start_sample=requested_start,
            pre_roll_truncated=(voice_onset_sample - actual_start) < self.pre_roll_samples,
        )
