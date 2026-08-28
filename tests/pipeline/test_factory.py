from __future__ import annotations

from pathlib import Path

import pytest

from lune.audio.transport import LocalAudioTransport
from lune.pipeline.factory import DeferredSTTSink
from lune.stt.contracts import FinalTranscript
from tests.pipeline.harness import build_harness


@pytest.mark.asyncio
async def test_one_cancel_moves_every_assembled_stage_to_the_new_generation(
    tmp_path: Path,
) -> None:
    harness = build_harness(tmp_path)
    pipeline = harness.pipeline
    await pipeline.session.start()

    event = await pipeline.coordinator.cancel("device_changed")

    assert event.clean is True
    assert pipeline.coordinator.generation_id == 1
    assert harness.stt.generation_id == 1
    assert pipeline.turn_gate.generation_id == 1
    assert pipeline.playback.is_stopped(0) is True
    assert harness.fence.interrupted == [0]
    # M5 routes cancellation to whichever backend owns the utterance; with none
    # in flight there is nothing to stop, which is why the fence still advanced.
    assert harness.backend.cancelled == []
    await pipeline.session.close()


@pytest.mark.asyncio
async def test_a_configured_transport_is_rebuilt_only_when_the_stream_is_unsafe(
    tmp_path: Path,
) -> None:
    from tests.pipeline.harness import SESSION_ID  # noqa: F401

    transport = LocalAudioTransport()
    transport.set_microphone(True)
    harness = build_harness(tmp_path)
    coordinator = harness.pipeline.coordinator
    coordinator._transport = transport  # type: ignore[attr-defined]

    await coordinator.cancel("barge_in")
    assert transport.microphone_enabled is True

    await coordinator.cancel("device_changed")
    assert transport.microphone_enabled is False
    await harness.pipeline.session.close()


@pytest.mark.asyncio
async def test_the_deferred_sink_binds_exactly_once() -> None:
    sink = DeferredSTTSink()
    event = FinalTranscript(request_id="r", generation_id=0, text="hello")

    with pytest.raises(RuntimeError):
        await sink(event)

    seen: list[FinalTranscript] = []

    async def target(value: object) -> None:
        assert isinstance(value, FinalTranscript)
        seen.append(value)

    sink.bind(target)
    await sink(event)
    assert seen == [event]

    with pytest.raises(RuntimeError):
        sink.bind(target)
