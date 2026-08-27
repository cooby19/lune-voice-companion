from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from lune.tts.avspeech import AVSpeechAdapter, NativePCM, _buffer_to_pcm
from lune.tts.contracts import PCMChunk, TTSBackendError, TTSLanguageHint, TTSRequest


class FakeAVSpeechDriver:
    def __init__(self) -> None:
        self.callback: Callable[[NativePCM | None], None] | None = None
        self.started: list[tuple[str, TTSLanguageHint | None]] = []
        self.stop_calls = 0
        self.closed = False

    def start(
        self,
        text: str,
        language_hint: TTSLanguageHint | None,
        callback: Callable[[NativePCM | None], None],
    ) -> None:
        self.started.append((text, language_hint))
        self.callback = callback

    def emit(self, item: NativePCM | None) -> None:
        assert self.callback is not None
        self.callback(item)

    def stop(self) -> None:
        self.stop_calls += 1

    def close(self) -> None:
        self.closed = True


class _FakeFormat:
    def __init__(self, common_format: int, channels: int, interleaved: bool) -> None:
        self._common_format = common_format
        self._channels = channels
        self._interleaved = interleaved

    def sampleRate(self) -> float:
        return 24_000.0

    def channelCount(self) -> int:
        return self._channels

    def commonFormat(self) -> int:
        return self._common_format

    def isInterleaved(self) -> bool:
        return self._interleaved


class _FakeBuffer:
    def __init__(self, values: tuple[tuple[float, ...], ...], *, interleaved: bool = False) -> None:
        self._values = values
        self._format = _FakeFormat(1, 2 if interleaved else len(values), interleaved)

    def format(self) -> _FakeFormat:
        return self._format

    def floatChannelData(self) -> tuple[tuple[float, ...], ...]:
        return self._values


def test_avspeech_float_buffers_become_interleaved_int16_pcm() -> None:
    buffer = _FakeBuffer(((-1.0, 0.5), (0.0, 1.0)))

    sample_rate, channels, data = _buffer_to_pcm(buffer, 2)

    assert sample_rate == 24_000
    assert channels == 2
    assert data == b"\x01\x80\x00\x00\x00@\xff\x7f"


@pytest.mark.asyncio
async def test_avspeech_streams_pcm_and_preserves_generation() -> None:
    driver = FakeAVSpeechDriver()
    backend = AVSpeechAdapter(driver)
    request = TTSRequest("r", 4, "hello", "en")

    async def collect() -> list[PCMChunk]:
        return [chunk async for chunk in backend.synthesize(request)]

    task = asyncio.create_task(collect())
    await asyncio.sleep(0)
    driver.emit((22_050, 1, b"\x01\x00\x02\x00"))
    driver.emit((22_050, 1, b"\x03\x00"))
    driver.emit(None)

    assert await task == [
        PCMChunk(4, 22_050, 1, b"\x01\x00\x02\x00"),
        PCMChunk(4, 22_050, 1, b"\x03\x00"),
    ]
    assert driver.started == [("hello", "en")]


@pytest.mark.asyncio
async def test_avspeech_cancel_stops_immediately_and_drops_late_pcm() -> None:
    driver = FakeAVSpeechDriver()
    backend = AVSpeechAdapter(driver)
    request = TTSRequest("r", 5, "private", "zh")
    chunks: list[PCMChunk] = []

    async def collect() -> None:
        async for chunk in backend.synthesize(request):
            chunks.append(chunk)

    task = asyncio.create_task(collect())
    await asyncio.sleep(0)
    await backend.cancel(5)
    driver.emit((22_050, 1, b"\x01\x00"))

    await task
    assert chunks == []
    assert driver.stop_calls == 1


@pytest.mark.asyncio
async def test_avspeech_cancel_clears_a_full_queue_without_failure() -> None:
    driver = FakeAVSpeechDriver()
    backend = AVSpeechAdapter(driver, queue_capacity=1)
    request = TTSRequest("r", 55, "private", "zh")
    chunks: list[PCMChunk] = []

    async def collect() -> None:
        async for chunk in backend.synthesize(request):
            chunks.append(chunk)

    task = asyncio.create_task(collect())
    await asyncio.sleep(0)
    driver.emit((22_050, 1, b"\x01\x00"))
    await backend.cancel(55)
    driver.emit((22_050, 1, b"\x02\x00"))

    await task
    assert chunks == []


@pytest.mark.asyncio
async def test_avspeech_queue_overflow_is_finite_failure() -> None:
    driver = FakeAVSpeechDriver()
    backend = AVSpeechAdapter(driver, queue_capacity=1)
    request = TTSRequest("r", 6, "hello")

    async def collect() -> list[PCMChunk]:
        return [chunk async for chunk in backend.synthesize(request)]

    task = asyncio.create_task(collect())
    await asyncio.sleep(0)
    driver.emit((22_050, 1, b"\x01\x00"))
    driver.emit((22_050, 1, b"\x02\x00"))
    await asyncio.sleep(0)

    with pytest.raises(TTSBackendError, match="synthesis_failed"):
        await task
    assert driver.stop_calls == 1


@pytest.mark.asyncio
async def test_avspeech_close_is_idempotent() -> None:
    driver = FakeAVSpeechDriver()
    backend = AVSpeechAdapter(driver)
    await backend.close()
    await backend.close()
    assert driver.closed

    with pytest.raises(TTSBackendError, match="backend_unavailable"):
        await anext(backend.synthesize(TTSRequest("r", 1, "hello")))
