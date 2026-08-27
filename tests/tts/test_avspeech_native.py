"""Native AVSpeech coverage: the run-loop pump, and real synthesis when it is available.

``tests/tts/test_avspeech.py`` injects a fake driver, so it never exercises the
CFRunLoop that ``writeUtterance_toBufferCallback_`` actually delivers buffers on. The
integration tests here drive the real ``_NativeAVSpeechDriver`` and skip themselves
when AVFoundation or its voices are missing.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from lune.tts.avspeech import _RUN_LOOP_HANDLED_SOURCE, AVSpeechAdapter, _MainRunLoopPump
from lune.tts.contracts import PCMChunk, TTSBackendError, TTSRequest

_RUN_LOOP_TIMED_OUT = 3
_FIRST_CHUNK_TIMEOUT_SECONDS = 10.0
_UTTERANCE_TIMEOUT_SECONDS = 30.0
_ZH_SENTENCE = "這是一段用來驗證串流的中文測試語音。"


def _speech_synthesis_available() -> bool:
    if os.environ.get("LUNE_SKIP_AVSPEECH_INTEGRATION"):
        return False
    try:
        import AVFoundation  # type: ignore[import-untyped]
        import CoreFoundation  # type: ignore[import-untyped]  # noqa: F401
    except ImportError:
        return False
    return bool(AVFoundation.AVSpeechSynthesisVoice.speechVoices())


needs_speech_synthesis = pytest.mark.skipif(
    not _speech_synthesis_available(),
    reason="AVFoundation speech synthesis is unavailable on this machine",
)


def test_run_loop_pump_requires_a_running_event_loop() -> None:
    pump = _MainRunLoopPump(lambda mode, seconds, once: _RUN_LOOP_TIMED_OUT, object(), lambda: None)

    with pytest.raises(TTSBackendError, match="backend_unavailable"):
        pump.start()


async def test_run_loop_pump_drains_a_bounded_batch_and_stops() -> None:
    drains = 0
    boundaries = 0
    batch_done = asyncio.Event()

    def run_in_mode(mode: object, seconds: float, once: bool) -> int:
        nonlocal drains
        drains += 1
        assert seconds == 0.0
        assert once is True
        return _RUN_LOOP_HANDLED_SOURCE

    def after_drain() -> None:
        nonlocal boundaries
        boundaries += 1
        if boundaries == 3:
            batch_done.set()

    pump = _MainRunLoopPump(run_in_mode, object(), after_drain, interval=0.05, drain_limit=3)
    pump.start()
    await asyncio.wait_for(batch_done.wait(), timeout=5.0)
    pump.stop()
    drained_at_stop = drains

    await asyncio.sleep(0.15)
    assert drains == 3
    assert boundaries == 3
    assert drains == drained_at_stop


async def test_run_loop_pump_ends_a_batch_once_no_source_is_handled() -> None:
    boundaries = 0
    idle = asyncio.Event()

    def run_in_mode(mode: object, seconds: float, once: bool) -> int:
        return _RUN_LOOP_TIMED_OUT

    def after_drain() -> None:
        nonlocal boundaries
        boundaries += 1
        idle.set()

    pump = _MainRunLoopPump(run_in_mode, object(), after_drain, interval=0.05, drain_limit=8)
    pump.start()
    await asyncio.wait_for(idle.wait(), timeout=5.0)
    pump.stop()

    # An idle run loop costs one drain per tick, never the whole batch.
    assert boundaries == 1


@pytest.mark.integration
@needs_speech_synthesis
async def test_native_driver_streams_real_pcm_within_a_bounded_timeout() -> None:
    """Fails against a driver that never runs the main CFRunLoop: no buffer ever lands."""

    backend = AVSpeechAdapter()
    stream = backend.synthesize(TTSRequest("integration", 7, _ZH_SENTENCE, "zh"))
    try:
        chunk = await asyncio.wait_for(anext(stream), timeout=_FIRST_CHUNK_TIMEOUT_SECONDS)
    finally:
        await stream.aclose()
        await backend.close()

    assert chunk.generation_id == 7
    assert chunk.data


@pytest.mark.integration
@needs_speech_synthesis
async def test_native_driver_streams_a_whole_utterance_under_the_bounded_queue() -> None:
    backend = AVSpeechAdapter()
    chunks: list[PCMChunk] = []
    try:
        async with asyncio.timeout(_UTTERANCE_TIMEOUT_SECONDS):
            async for chunk in backend.synthesize(TTSRequest("integration", 8, _ZH_SENTENCE, "zh")):
                chunks.append(chunk)
    finally:
        await backend.close()

    assert chunks
    assert {chunk.generation_id for chunk in chunks} == {8}
    assert {chunk.sample_rate for chunk in chunks} == {chunks[0].sample_rate}
    assert all(len(chunk.data) % (2 * chunk.channels) == 0 for chunk in chunks)


@pytest.mark.integration
@needs_speech_synthesis
async def test_native_driver_keeps_a_cancelled_utterance_out_of_the_next_generation() -> None:
    backend = AVSpeechAdapter()
    cancelled: list[PCMChunk] = []
    resumed: list[PCMChunk] = []
    try:
        async with asyncio.timeout(_UTTERANCE_TIMEOUT_SECONDS):
            async for chunk in backend.synthesize(TTSRequest("integration", 9, _ZH_SENTENCE, "zh")):
                cancelled.append(chunk)
                await backend.cancel(9)
            async for chunk in backend.synthesize(TTSRequest("integration", 10, "測試。", "zh")):
                resumed.append(chunk)
    finally:
        await backend.close()

    assert {chunk.generation_id for chunk in cancelled} == {9}
    assert resumed
    assert {chunk.generation_id for chunk in resumed} == {10}
