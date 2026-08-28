from __future__ import annotations

import pytest

from lune.pipeline.benchmark import (
    InterruptionSample,
    TurnLatencySample,
    evaluate_end_to_end,
    evaluate_interruption,
)


def sample(index: int, latency_ms: float) -> TurnLatencySample:
    return TurnLatencySample(
        generation_id=index,
        speech_end_at=100.0,
        first_audible_at=100.0 + latency_ms / 1_000.0,
    )


def warm_run(latency_ms: float, *, turns: int = 30) -> list[TurnLatencySample]:
    return [sample(index, latency_ms) for index in range(turns)]


def test_missing_evidence_fails_instead_of_passing_silently() -> None:
    gate = evaluate_end_to_end([])
    assert gate.evaluated is False
    assert gate.passed is False
    assert gate.reasons == ("no_samples",)
    assert gate.p50_ms is None


def test_a_warm_run_inside_both_budgets_passes() -> None:
    gate = evaluate_end_to_end(warm_run(1_200.0))
    assert gate.passed is True
    assert gate.reasons == ()
    assert gate.turns == 30
    assert gate.delivered == 30
    assert gate.p50_ms == pytest.approx(1_200.0)
    assert gate.p95_ms == pytest.approx(1_200.0)


def test_fewer_than_thirty_warm_turns_cannot_pass() -> None:
    gate = evaluate_end_to_end(warm_run(1_000.0, turns=29))
    assert gate.passed is False
    assert "turns_insufficient" in gate.reasons


def test_a_turn_that_never_produced_audio_fails_the_gate() -> None:
    samples = warm_run(1_000.0)
    samples[7] = TurnLatencySample(generation_id=7, speech_end_at=100.0)
    gate = evaluate_end_to_end(samples)
    assert gate.passed is False
    assert "undelivered_turns" in gate.reasons
    assert gate.delivered == 29


def test_the_median_and_tail_budgets_are_reported_separately() -> None:
    samples = [*warm_run(1_400.0, turns=28), sample(28, 3_000.0), sample(29, 3_100.0)]
    gate = evaluate_end_to_end(samples)
    assert gate.passed is False
    assert gate.reasons == ("p95_exceeded",)
    assert gate.p50_ms == pytest.approx(1_400.0)
    assert gate.p95_ms == pytest.approx(3_000.0)
    assert gate.max_ms == pytest.approx(3_100.0)


def test_a_slow_median_is_reported_even_when_the_tail_would_pass() -> None:
    gate = evaluate_end_to_end(warm_run(1_900.0))
    assert set(gate.reasons) == {"p50_exceeded"}


def test_the_percentile_never_interpolates_a_value_that_was_not_measured() -> None:
    latencies = [100.0, 200.0, 300.0, 400.0]
    gate = evaluate_end_to_end(
        [sample(index, value) for index, value in enumerate(latencies)],
        required_turns=1,
    )
    assert gate.p50_ms == pytest.approx(200.0)
    assert gate.p95_ms == pytest.approx(400.0)


def test_output_must_not_precede_the_end_of_the_input_speech() -> None:
    with pytest.raises(ValueError):
        TurnLatencySample(generation_id=1, speech_end_at=5.0, first_audible_at=4.0)


def test_interruptions_pass_only_when_every_trial_stops_inside_the_budget() -> None:
    gate = evaluate_interruption(
        [InterruptionSample(generation_id=index, audible_stop_ms=80.0) for index in range(5)]
    )
    assert gate.passed is True
    assert gate.trials == 5
    assert gate.max_ms == pytest.approx(80.0)

    slow = evaluate_interruption(
        [
            InterruptionSample(generation_id=1, audible_stop_ms=80.0),
            InterruptionSample(generation_id=2, audible_stop_ms=240.0),
        ]
    )
    assert slow.passed is False
    assert slow.reasons == ("stop_exceeded",)


def test_any_event_after_the_fence_fails_the_interruption_gate() -> None:
    gate = evaluate_interruption(
        [InterruptionSample(generation_id=1, audible_stop_ms=10.0, late_events=1)]
    )
    assert gate.passed is False
    assert gate.reasons == ("late_events",)
    assert gate.late_events == 1


def test_an_interruption_gate_without_trials_is_not_a_pass() -> None:
    gate = evaluate_interruption([])
    assert (gate.evaluated, gate.passed) == (False, False)
    assert gate.reasons == ("no_samples",)
