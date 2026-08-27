"""PCM-streaming adapter for macOS ``AVSpeechSynthesizer``."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from lune.tts.contracts import PCMChunk, TTSBackendError, TTSLanguageHint, TTSRequest

type NativePCM = tuple[int, int, bytes]
type NativeItem = NativePCM | TTSBackendError | None
type NativeCallback = Callable[[NativeItem], None]


class AVSpeechDriver(Protocol):
    def start(
        self,
        text: str,
        language_hint: TTSLanguageHint | None,
        callback: NativeCallback,
    ) -> None: ...

    def stop(self) -> None: ...

    def close(self) -> None: ...


class _NativeAVSpeechDriver:
    """Small lazy PyObjC boundary; importing the module is safe off macOS."""

    def __init__(self) -> None:
        try:
            import AVFoundation  # type: ignore[import-untyped]
        except ImportError as error:
            raise TTSBackendError("backend_unavailable") from error
        self._av: Any = AVFoundation
        self._synthesizer: Any = AVFoundation.AVSpeechSynthesizer.alloc().init()
        self._callback: NativeCallback | None = None
        self._native_block: Callable[[Any], None] | None = None

    def start(
        self,
        text: str,
        language_hint: TTSLanguageHint | None,
        callback: NativeCallback,
    ) -> None:
        self._callback = callback
        utterance = self._av.AVSpeechUtterance.speechUtteranceWithString_(text)
        language = "en-US" if language_hint == "en" else "zh-TW"
        voice = self._av.AVSpeechSynthesisVoice.voiceWithLanguage_(language)
        if voice is not None:
            utterance.setVoice_(voice)

        def receive(buffer: Any) -> None:
            active = self._callback
            if active is None:
                return
            frame_length = int(buffer.frameLength())
            if frame_length == 0:
                active(None)
                return
            try:
                active(_buffer_to_pcm(buffer, frame_length))
            except TTSBackendError as error:
                active(error)

        self._native_block = receive
        self._synthesizer.writeUtterance_toBufferCallback_(utterance, receive)

    def stop(self) -> None:
        self._synthesizer.stopSpeakingAtBoundary_(self._av.AVSpeechBoundaryImmediate)

    def close(self) -> None:
        self.stop()
        self._callback = None
        self._native_block = None


def _buffer_to_pcm(buffer: Any, frame_length: int) -> NativePCM:
    """Copy an AVAudioPCMBuffer into interleaved little-endian signed 16-bit PCM."""

    audio_format = buffer.format()
    sample_rate = round(float(audio_format.sampleRate()))
    channels = int(audio_format.channelCount())
    common_format = int(audio_format.commonFormat())
    interleaved = bool(audio_format.isInterleaved())
    if frame_length <= 0 or channels <= 0:
        raise TTSBackendError("synthesis_failed")

    if common_format == 1:
        channel_data = buffer.floatChannelData()
        values = _copy_channels(channel_data, frame_length, channels, interleaved, np.float32)
        pcm = np.rint(np.clip(values, -1.0, 1.0) * 32767.0).astype("<i2")
    elif common_format == 3:
        channel_data = buffer.int16ChannelData()
        pcm = _copy_channels(channel_data, frame_length, channels, interleaved, np.int16).astype(
            "<i2", copy=False
        )
    elif common_format == 4:
        channel_data = buffer.int32ChannelData()
        values = _copy_channels(channel_data, frame_length, channels, interleaved, np.int32)
        pcm = np.right_shift(values.astype(np.int64), 16).astype("<i2")
    else:
        raise TTSBackendError("synthesis_failed")
    return sample_rate, channels, pcm.tobytes(order="C")


def _copy_channels(
    channel_data: Any,
    frame_length: int,
    channels: int,
    interleaved: bool,
    dtype: np.dtype[Any] | type[np.generic],
) -> NDArray[Any]:
    if channel_data is None:
        raise TTSBackendError("synthesis_failed")
    try:
        if interleaved:
            copied = np.asarray(channel_data[0][: frame_length * channels], dtype=dtype)
            return copied.reshape(frame_length, channels)
        copied_channels = [
            np.asarray(channel_data[index][:frame_length], dtype=dtype) for index in range(channels)
        ]
        return np.stack(copied_channels, axis=1)
    except (IndexError, TypeError, ValueError) as error:
        raise TTSBackendError("synthesis_failed") from error


class AVSpeechAdapter:
    """Expose AVSpeech callbacks as generation-fenced PCM async iteration."""

    def __init__(
        self,
        driver: AVSpeechDriver | None = None,
        *,
        queue_capacity: int = 32,
    ) -> None:
        if queue_capacity < 1:
            raise ValueError("queue capacity must be positive")
        self._driver = driver
        self._queue_capacity = queue_capacity
        self._closed = False
        self._active_generation: int | None = None
        self._active_queue: asyncio.Queue[NativeItem] | None = None
        self._cancelled: set[int] = set()
        self._lock = asyncio.Lock()

    def _get_driver(self) -> AVSpeechDriver:
        if self._driver is None:
            self._driver = _NativeAVSpeechDriver()
        return self._driver

    async def synthesize(self, request: TTSRequest) -> AsyncIterator[PCMChunk]:
        async with self._lock:
            if self._closed:
                raise TTSBackendError("backend_unavailable")
            self._cancelled.discard(request.generation_id)
            queue: asyncio.Queue[NativeItem] = asyncio.Queue(maxsize=self._queue_capacity)
            self._active_generation = request.generation_id
            self._active_queue = queue
            loop = asyncio.get_running_loop()

            def enqueue(item: NativeItem) -> None:
                def put() -> None:
                    if self._active_queue is not queue:
                        return
                    if request.generation_id in self._cancelled:
                        if item is None and queue.empty():
                            queue.put_nowait(None)
                        return
                    if queue.full():
                        while not queue.empty():
                            queue.get_nowait()
                        queue.put_nowait(TTSBackendError("synthesis_failed"))
                        self._get_driver().stop()
                        return
                    queue.put_nowait(item)

                loop.call_soon_threadsafe(put)

            try:
                self._get_driver().start(request.text, request.language_hint, enqueue)
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    if isinstance(item, TTSBackendError):
                        raise item
                    if request.generation_id in self._cancelled:
                        continue
                    sample_rate, channels, data = item
                    yield PCMChunk(
                        generation_id=request.generation_id,
                        sample_rate=sample_rate,
                        channels=channels,
                        data=data,
                    )
            except TTSBackendError:
                raise
            except Exception as error:
                raise TTSBackendError("synthesis_failed") from error
            finally:
                if self._active_queue is queue:
                    self._active_queue = None
                    self._active_generation = None

    async def cancel(self, generation_id: int) -> None:
        self._cancelled.add(generation_id)
        if self._active_generation != generation_id:
            return
        self._get_driver().stop()
        queue = self._active_queue
        if queue is not None:
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._active_generation is not None:
            await self.cancel(self._active_generation)
        if self._driver is not None:
            self._driver.close()
