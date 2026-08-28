from __future__ import annotations

import asyncio

import pytest

from lune.pipeline.playback import PlaybackSink
from lune.tts.contracts import PCMChunk
from tests.pipeline.conftest import RecordingOutputDevice, pcm_chunk


class BlockingOutputDevice(RecordingOutputDevice):
    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def write(self, chunk: PCMChunk) -> None:
        await self.gate.wait()
        await super().write(chunk)


class FailingOutputDevice(RecordingOutputDevice):
    async def write(self, chunk: PCMChunk) -> None:
        raise RuntimeError("device is gone")


@pytest.mark.asyncio
async def test_playback_writes_in_order_and_stamps_the_first_audible_frame() -> None:
    device = RecordingOutputDevice()
    ticks = iter([1.0, 2.0, 3.0])
    sink = PlaybackSink(device, monotonic=lambda: next(ticks))
    await sink.start()

    assert await sink.submit(pcm_chunk(4, amplitude=0)) is True
    assert await sink.submit(pcm_chunk(4, amplitude=9_000)) is True
    assert await sink.drain(4) is True

    assert device.generations() == [4, 4]
    assert sink.first_audible_at(4) == 1.0
    assert sink.first_audible_at(5) is None
    await sink.close()
    assert device.closed is True


@pytest.mark.asyncio
async def test_silence_never_counts_as_audible_output() -> None:
    device = RecordingOutputDevice()
    sink = PlaybackSink(device, silence_floor=100)
    await sink.start()

    await sink.submit(pcm_chunk(1, amplitude=50))
    assert await sink.drain(1) is True
    assert sink.first_audible_at(1) is None

    await sink.submit(pcm_chunk(1, amplitude=500))
    assert await sink.drain(1) is True
    assert sink.first_audible_at(1) is not None
    await sink.close()


@pytest.mark.asyncio
async def test_stopping_a_generation_purges_queued_audio_and_flushes_the_device() -> None:
    device = BlockingOutputDevice()
    sink = PlaybackSink(device)
    await sink.start()
    for _ in range(4):
        assert await sink.submit(pcm_chunk(7)) is True
    assert await sink.submit(pcm_chunk(8)) is True

    await sink.stop_generation(7)

    assert device.flushes == 1
    assert sink.is_stopped(7) is True
    assert await sink.submit(pcm_chunk(7)) is False
    assert await sink.submit(pcm_chunk(6)) is False
    assert await sink.submit(pcm_chunk(8)) is True

    device.gate.set()
    assert await sink.drain(8) is True
    assert set(device.generations()) == {8}
    await sink.close()


@pytest.mark.asyncio
async def test_draining_a_stopped_generation_reports_failure_instead_of_hanging() -> None:
    device = BlockingOutputDevice()
    sink = PlaybackSink(device)
    await sink.start()
    await sink.submit(pcm_chunk(2))

    drain = asyncio.create_task(sink.drain(2))
    await asyncio.sleep(0)
    await sink.stop_generation(2)

    assert await asyncio.wait_for(drain, timeout=1.0) is False
    device.gate.set()
    await sink.close()


@pytest.mark.asyncio
async def test_the_output_queue_is_bounded_and_reports_its_overflow() -> None:
    device = BlockingOutputDevice()
    sink = PlaybackSink(device, capacity=2)
    await sink.start()

    assert await sink.submit(pcm_chunk(1)) is True
    assert await sink.submit(pcm_chunk(1)) is True
    assert await sink.submit(pcm_chunk(1)) is False

    health = sink.health()
    assert health.overflowed is True
    assert health.dropped_chunks == 1
    assert health.queue_depth == 2

    device.gate.set()
    await sink.close()


@pytest.mark.asyncio
async def test_a_failing_device_is_counted_and_does_not_stall_playback() -> None:
    device = FailingOutputDevice()
    sink = PlaybackSink(device)
    await sink.start()

    await sink.submit(pcm_chunk(1))
    assert await sink.drain(1) is True
    assert sink.health().write_failures == 1
    assert device.written == []

    await sink.submit(pcm_chunk(1))
    assert await sink.drain(1) is True
    assert sink.health().write_failures == 2
    await sink.close()


@pytest.mark.asyncio
async def test_a_closed_sink_refuses_new_audio() -> None:
    device = RecordingOutputDevice()
    sink = PlaybackSink(device)
    await sink.start()
    await sink.close()

    assert sink.closed is True
    assert await sink.submit(pcm_chunk(1)) is False
    assert await sink.drain(1) is False
