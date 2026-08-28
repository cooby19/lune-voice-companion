from __future__ import annotations

import json
from pathlib import Path

import pytest

from lune.diagnostics import SafeDiagnostics
from lune.pipeline.contracts import CancelReason
from lune.pipeline.coordinator import GenerationCoordinator


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.failing: set[str] = set()

    def record(self, stage: str, value: object = None) -> None:
        self.calls.append((stage, value))
        if stage in self.failing:
            raise RuntimeError(stage)

    def stages(self) -> list[str]:
        return [stage for stage, _ in self.calls]


class FakePlayback:
    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder

    async def stop_generation(self, generation_id: int) -> None:
        self._recorder.record("playback", generation_id)


class FakeTTS:
    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder

    async def cancel(self, generation_id: int) -> None:
        self._recorder.record("tts", generation_id)


class FakeSTT:
    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder

    def set_generation(self, generation_id: int) -> None:
        self._recorder.record("stt", generation_id)


class FakeProvider:
    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder

    async def interrupt_and_drain(self, generation_id: int) -> None:
        self._recorder.record("provider", generation_id)


class FakeProposals:
    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder

    def cancel_generation(self, generation_id: int) -> int:
        self._recorder.record("proposals", generation_id)
        return 0


class FakeTurnGate:
    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder

    def reset_generation(self, generation_id: int) -> None:
        self._recorder.record("turn_gate_reset", generation_id)

    def carry_over_generation(self, generation_id: int) -> None:
        self._recorder.record("turn_gate_carry_over", generation_id)


class FakeTransport:
    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder

    def set_generation(self, generation_id: int) -> None:
        self._recorder.record("transport_set", generation_id)

    def rebuild(self, *, generation_id: int) -> None:
        self._recorder.record("transport_rebuild", generation_id)


def build(
    recorder: Recorder,
    *,
    diagnostics: SafeDiagnostics | None = None,
    clock: list[float] | None = None,
) -> GenerationCoordinator:
    ticks = iter(clock or [0.0, 0.05])
    return GenerationCoordinator(
        playback=FakePlayback(recorder),
        tts=FakeTTS(recorder),
        stt=FakeSTT(recorder),
        turn_gate=FakeTurnGate(recorder),
        proposals=FakeProposals(recorder),
        provider=FakeProvider(recorder),
        transport=FakeTransport(recorder),
        diagnostics=diagnostics,
        monotonic=lambda: next(ticks),
    )


@pytest.mark.asyncio
async def test_cancelling_advances_the_fence_before_any_teardown_runs() -> None:
    recorder = Recorder()
    coordinator = build(recorder)
    seen: list[int] = []

    class Observing(FakePlayback):
        async def stop_generation(self, generation_id: int) -> None:
            seen.append(coordinator.generation_id)
            await super().stop_generation(generation_id)

    coordinator = GenerationCoordinator(
        playback=Observing(recorder),
        tts=FakeTTS(recorder),
        stt=FakeSTT(recorder),
        turn_gate=FakeTurnGate(recorder),
        proposals=FakeProposals(recorder),
    )
    event = await coordinator.cancel("barge_in")

    assert seen == [1]
    assert event.previous_generation_id == 0
    assert event.generation_id == 1
    assert coordinator.is_current(1) is True
    assert coordinator.is_current(0) is False


@pytest.mark.asyncio
async def test_audible_output_is_stopped_before_every_other_stage() -> None:
    recorder = Recorder()
    coordinator = build(recorder)
    await coordinator.cancel("barge_in")

    assert recorder.stages() == [
        "playback",
        "tts",
        "stt",
        "provider",
        "proposals",
        "turn_gate_carry_over",
        "transport_set",
    ]
    assert recorder.calls[0][1] == 0
    assert recorder.calls[2][1] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "gate_stage", "transport_stage"),
    [
        ("barge_in", "turn_gate_carry_over", "transport_set"),
        ("device_changed", "turn_gate_reset", "transport_rebuild"),
        ("stt_timeout", "turn_gate_reset", "transport_set"),
        ("output_overflow", "turn_gate_reset", "transport_rebuild"),
        ("shutdown", "turn_gate_reset", "transport_set"),
        ("error", "turn_gate_reset", "transport_set"),
    ],
)
async def test_only_a_barge_in_carries_audio_across_the_fence(
    reason: CancelReason,
    gate_stage: str,
    transport_stage: str,
) -> None:
    recorder = Recorder()
    coordinator = build(recorder)
    await coordinator.cancel(reason)
    assert gate_stage in recorder.stages()
    assert transport_stage in recorder.stages()


@pytest.mark.asyncio
async def test_a_failing_stage_is_reported_without_skipping_the_rest() -> None:
    recorder = Recorder()
    recorder.failing = {"tts", "proposals"}
    coordinator = build(recorder)

    event = await coordinator.cancel("error")

    assert event.failed_stages == ("tts", "proposals")
    assert event.clean is False
    assert recorder.stages() == [
        "playback",
        "tts",
        "stt",
        "provider",
        "proposals",
        "turn_gate_reset",
        "transport_set",
    ]


@pytest.mark.asyncio
async def test_the_audible_stop_duration_is_measured_across_playback_and_tts() -> None:
    recorder = Recorder()
    coordinator = build(recorder, clock=[10.0, 10.15])
    event = await coordinator.cancel("barge_in")
    assert event.audible_stop_ms == pytest.approx(150.0)


@pytest.mark.asyncio
async def test_repeated_cancels_keep_advancing_and_never_reuse_a_generation() -> None:
    recorder = Recorder()
    coordinator = build(recorder, clock=[0.0, 0.0, 0.0, 0.0])
    first = await coordinator.cancel("barge_in")
    second = await coordinator.cancel("device_changed")

    assert (first.generation_id, second.previous_generation_id) == (1, 1)
    assert second.generation_id == 2


@pytest.mark.asyncio
async def test_diagnostics_record_the_cancel_without_any_private_field(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "lune.jsonl"
    recorder = Recorder()
    coordinator = build(recorder, diagnostics=SafeDiagnostics(log_path), clock=[0.0, 0.02])

    await coordinator.cancel("barge_in")

    entry = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert entry == {
        "event": "cancel_barge_in",
        "generation_id": 1,
        "duration_ms": 20.0,
        "count": 0,
    }
