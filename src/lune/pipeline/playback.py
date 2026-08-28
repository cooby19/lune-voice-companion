"""Bounded, generation-fenced PCM playback with a measurable interruption stop."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from lune.tts.contracts import PCMChunk

DEFAULT_SILENCE_FLOOR = 32
_MAX_TRACKED_GENERATIONS = 128


class AudioOutputDevice(Protocol):
    """The playback half of the local transport; M7 owns the CoreAudio stream."""

    async def write(self, chunk: PCMChunk) -> None: ...

    async def flush(self) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PlaybackHealth:
    queue_depth: int
    dropped_chunks: int
    overflowed: bool
    write_failures: int


class PlaybackSink:
    """Accept PCM for the current generation only and drop everything older.

    Generations are monotonic, so a single high-water mark fences playback in
    constant space: stopping generation *n* makes every chunk at or below *n*
    unplayable, whether it is already queued or still being synthesised.
    """

    def __init__(
        self,
        device: AudioOutputDevice,
        *,
        capacity: int = 32,
        silence_floor: int = DEFAULT_SILENCE_FLOOR,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1:
            raise ValueError("playback capacity must be positive")
        if silence_floor < 0:
            raise ValueError("silence floor cannot be negative")
        self._device = device
        self._queue: asyncio.Queue[PCMChunk] = asyncio.Queue(maxsize=capacity)
        self._silence_floor = silence_floor
        self._monotonic = monotonic
        self._stopped_before = 0
        self._pending: dict[int, int] = {}
        self._idle: dict[int, asyncio.Event] = {}
        self._first_audible: dict[int, float] = {}
        self._dropped_chunks = 0
        self._write_failures = 0
        self._overflowed = False
        self._writer: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def health(self) -> PlaybackHealth:
        return PlaybackHealth(
            queue_depth=self._queue.qsize(),
            dropped_chunks=self._dropped_chunks,
            overflowed=self._overflowed,
            write_failures=self._write_failures,
        )

    def is_stopped(self, generation_id: int) -> bool:
        return generation_id < self._stopped_before

    def first_audible_at(self, generation_id: int) -> float | None:
        """When the first non-silent frame reached the device, if it ever did."""

        return self._first_audible.get(generation_id)

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("playback sink is closed")
        if self._writer is None or self._writer.done():
            self._writer = asyncio.create_task(self._run(), name="lune-playback")

    async def submit(self, chunk: PCMChunk) -> bool:
        """Return False when the chunk is stale or the bounded queue overflowed."""

        if self._closed or self.is_stopped(chunk.generation_id):
            return False
        if self._writer is None or self._writer.done():
            await self.start()
        try:
            self._queue.put_nowait(chunk)
        except asyncio.QueueFull:
            self._dropped_chunks += 1
            self._overflowed = True
            return False
        self._pending[chunk.generation_id] = self._pending.get(chunk.generation_id, 0) + 1
        return True

    async def stop_generation(self, generation_id: int) -> None:
        """Close the fence, purge queued audio, and flush the device buffer."""

        self._stopped_before = max(self._stopped_before, generation_id + 1)
        retained: list[PCMChunk] = []
        while True:
            try:
                queued = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if self.is_stopped(queued.generation_id):
                self._release(queued.generation_id)
                continue
            retained.append(queued)
        for chunk in retained:
            self._queue.put_nowait(chunk)
        for pending in [key for key in self._pending if self.is_stopped(key)]:
            self._pending.pop(pending, None)
            self._wake(pending)
        await self._device.flush()

    async def drain(self, generation_id: int) -> bool:
        """Wait until this generation's queued audio reached the device."""

        if self.is_stopped(generation_id) or self._closed:
            return False
        while self._pending.get(generation_id, 0) > 0:
            await self._wait_event(generation_id).wait()
            if self.is_stopped(generation_id) or self._closed:
                return False
        return True

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        writer = self._writer
        self._writer = None
        if writer is not None and not writer.done():
            writer.cancel()
            try:
                await writer
            except asyncio.CancelledError:
                pass
        for generation_id in list(self._pending):
            self._pending.pop(generation_id, None)
            self._wake(generation_id)
        await self._device.close()

    async def _run(self) -> None:
        while not self._closed:
            chunk = await self._queue.get()
            generation_id = chunk.generation_id
            if self.is_stopped(generation_id):
                self._release(generation_id)
                continue
            try:
                await self._device.write(chunk)
            except asyncio.CancelledError:
                self._release(generation_id)
                raise
            except Exception:
                self._write_failures += 1
                self._release(generation_id)
                continue
            if generation_id not in self._first_audible and self._is_audible(chunk):
                self._record_audible(generation_id)
            self._release(generation_id)

    def _is_audible(self, chunk: PCMChunk) -> bool:
        samples = np.frombuffer(chunk.data, dtype="<i2")
        if samples.size == 0:
            return False
        return bool(np.abs(samples.astype(np.int32)).max() > self._silence_floor)

    def _record_audible(self, generation_id: int) -> None:
        self._first_audible[generation_id] = self._monotonic()
        if len(self._first_audible) > _MAX_TRACKED_GENERATIONS:
            for stale in sorted(self._first_audible)[:-_MAX_TRACKED_GENERATIONS]:
                self._first_audible.pop(stale, None)

    def _release(self, generation_id: int) -> None:
        remaining = self._pending.get(generation_id, 0) - 1
        if remaining > 0:
            self._pending[generation_id] = remaining
            return
        self._pending.pop(generation_id, None)
        self._wake(generation_id)

    def _wait_event(self, generation_id: int) -> asyncio.Event:
        event = self._idle.get(generation_id)
        if event is None:
            event = asyncio.Event()
            self._idle[generation_id] = event
        return event

    def _wake(self, generation_id: int) -> None:
        event = self._idle.pop(generation_id, None)
        if event is not None:
            event.set()
