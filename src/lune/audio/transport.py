"""Callback-safe bounded local PCM transport; microphone starts disabled."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from lune.audio.types import BYTES_PER_SAMPLE, AudioSpan


@dataclass(frozen=True, slots=True)
class TransportHealth:
    queue_depth: int
    overflowed: bool
    dropped_callbacks: int


class LocalAudioTransport:
    """The PyAudio callback only copies into this bounded queue and returns."""

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        channels: int = 1,
        max_callbacks: int = 32,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if sample_rate <= 0 or channels <= 0 or max_callbacks <= 0:
            raise ValueError("invalid transport settings")
        self._monotonic = monotonic
        self.sample_rate = sample_rate
        self.channels = channels
        self._queue: queue.Queue[AudioSpan] = queue.Queue(maxsize=max_callbacks)
        self._microphone_enabled = False
        self._sample_cursor = 0
        self._generation_id = 0
        self._overflowed = threading.Event()
        self._dropped_callbacks = 0
        self._anchor: tuple[int, float] | None = None
        self._state_lock = threading.Lock()

    @property
    def microphone_enabled(self) -> bool:
        with self._state_lock:
            return self._microphone_enabled

    def set_microphone(self, enabled: bool) -> None:
        with self._state_lock:
            self._microphone_enabled = enabled

    def set_generation(self, generation_id: int) -> None:
        if generation_id < 0:
            raise ValueError("generation ID cannot be negative")
        with self._state_lock:
            self._generation_id = generation_id

    def audio_callback(self, pcm: bytes) -> bool:
        """Return quickly; False tells the lifecycle owner to rebuild the stream."""

        bytes_per_frame = self.channels * BYTES_PER_SAMPLE
        if not pcm or len(pcm) % bytes_per_frame:
            self._overflowed.set()
            return False
        frame_count = len(pcm) // bytes_per_frame
        pcm_copy = bytes(pcm)
        # Stamp arrival before any queueing: the callback fires once the driver
        # has captured this buffer, so it is the only place where a sample
        # offset can still be tied to wall-clock time.
        arrived_at = self._monotonic()
        with self._state_lock:
            start = self._sample_cursor
            self._sample_cursor += frame_count
            self._anchor = (self._sample_cursor, arrived_at)
            if not self._microphone_enabled:
                return True
            span = AudioSpan(
                pcm=pcm_copy,
                start_sample=start,
                end_sample=start + frame_count,
                generation_id=self._generation_id,
                sample_rate=self.sample_rate,
                channels=self.channels,
            )
            try:
                self._queue.put_nowait(span)
            except queue.Full:
                self._dropped_callbacks += 1
                self._overflowed.set()
                return False
        return True

    def read_nowait(self) -> AudioSpan | None:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def health(self) -> TransportHealth:
        with self._state_lock:
            dropped_callbacks = self._dropped_callbacks
        return TransportHealth(
            queue_depth=self._queue.qsize(),
            overflowed=self._overflowed.is_set(),
            dropped_callbacks=dropped_callbacks,
        )

    def wall_time_of_sample(self, sample: int) -> float | None:
        """Map an absolute sample offset back to when the microphone captured it.

        The end-to-end clock starts at the last voiced *sample*, so it cannot be
        derived from processing time: whenever the pipeline trails the device,
        a processing-time estimate lands after the speech really ended and makes
        the measured latency look shorter than it was.
        """

        with self._state_lock:
            anchor = self._anchor
        if anchor is None:
            return None
        anchor_sample, anchor_time = anchor
        return anchor_time - (anchor_sample - sample) / self.sample_rate

    def mark_discontinuity(self) -> None:
        """Flag a PortAudio status/format discontinuity for lifecycle recovery."""

        self._overflowed.set()

    def rebuild(self, *, generation_id: int) -> None:
        """Reset a corrupt stream after the generation coordinator has cancelled it."""

        with self._state_lock:
            self._microphone_enabled = False
            while self.read_nowait() is not None:
                pass
            self._sample_cursor = 0
            self._generation_id = generation_id
            self._dropped_callbacks = 0
            self._anchor = None
            self._overflowed.clear()
