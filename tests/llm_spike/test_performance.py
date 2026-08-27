from __future__ import annotations

import pytest

from lune.llm_spike.performance import (
    DEFAULT_COMBINED_PEAK_RSS_LIMIT_BYTES,
    MIN_STABILITY_TURNS,
    LatencyBudget,
    LocalLLMMeasurements,
    detect_growth,
    evaluate_performance,
    nearest_rank,
)

STEADY = (100, 102, 99, 101, 100, 103)
DERIVED_BUDGET = LatencyBudget(stt_final_p50_ms=300.0, tts_ttfa_p50_ms=400.0)


def passing_measurements(**overrides: object) -> LocalLLMMeasurements:
    turns = MIN_STABILITY_TURNS
    base: dict[str, object] = {
        "turns": turns,
        "prompt_processing_ms": tuple(80.0 for _ in range(turns)),
        "first_token_ms": tuple(120.0 for _ in range(turns)),
        "first_sentence_ms": tuple(400.0 for _ in range(turns)),
        "output_tokens_per_second": tuple(45.0 for _ in range(turns)),
        "cold_start_ms": 2_500.0,
        "warm_start_ms": 300.0,
        "peak_rss_bytes": 7 * 1024**3,
        "rss_samples": STEADY,
        "swap_used_bytes": (0, 0, 0, 0, 0, 0),
        "queue_depth": (0, 1, 0, 1, 0, 0),
        "memory_pressure": tuple("normal" for _ in range(6)),
        "thermal_states": ("nominal", "nominal", "fair", "nominal", "fair", "nominal"),
    }
    base.update(overrides)
    return LocalLLMMeasurements(**base)  # type: ignore[arg-type]


def test_budget_is_underived_until_upstream_gates_have_run() -> None:
    budget = LatencyBudget()
    assert not budget.derived
    assert budget.first_sentence_budget_ms() is None


def test_budget_subtracts_silence_stt_and_tts_from_the_end_to_end_target() -> None:
    assert DERIVED_BUDGET.derived
    assert DERIVED_BUDGET.first_sentence_budget_ms() == pytest.approx(450.0)


def test_budget_never_goes_negative() -> None:
    budget = LatencyBudget(stt_final_p50_ms=900.0, tts_ttfa_p50_ms=900.0)
    assert budget.first_sentence_budget_ms() == 0.0


def test_unrun_spike_is_not_a_pass() -> None:
    gate = evaluate_performance(None)
    assert not gate.evaluated
    assert not gate.passed
    assert gate.aggregates is None


def test_complete_run_within_budget_passes() -> None:
    gate = evaluate_performance(passing_measurements(), budget=DERIVED_BUDGET)
    assert gate.passed, gate.reasons
    assert gate.aggregates is not None
    assert gate.aggregates.first_sentence_budget_ms == pytest.approx(450.0)
    assert gate.aggregates.turns == MIN_STABILITY_TURNS


def test_underived_budget_fails_instead_of_passing_silently() -> None:
    gate = evaluate_performance(passing_measurements())
    assert not gate.passed
    assert "first_sentence_budget_underived" in gate.reasons


def test_slow_first_sentence_fails_the_budget() -> None:
    slow = passing_measurements(first_sentence_ms=tuple(900.0 for _ in range(MIN_STABILITY_TURNS)))
    gate = evaluate_performance(slow, budget=DERIVED_BUDGET)
    assert "first_sentence_budget_exceeded" in gate.reasons


def test_short_run_fails_the_stability_requirement() -> None:
    short = passing_measurements(
        turns=10,
        prompt_processing_ms=tuple(80.0 for _ in range(10)),
        first_token_ms=tuple(120.0 for _ in range(10)),
        first_sentence_ms=tuple(400.0 for _ in range(10)),
        output_tokens_per_second=tuple(45.0 for _ in range(10)),
    )
    gate = evaluate_performance(short, budget=DERIVED_BUDGET)
    assert "turns_insufficient" in gate.reasons


def test_missing_start_up_samples_fail() -> None:
    gate = evaluate_performance(
        passing_measurements(cold_start_ms=None, warm_start_ms=None), budget=DERIVED_BUDGET
    )
    assert "cold_start_missing" in gate.reasons
    assert "warm_start_missing" in gate.reasons


def test_memory_and_thermal_states_must_stay_safe() -> None:
    gate = evaluate_performance(
        passing_measurements(
            memory_pressure=("normal", "normal", "warn", "normal", "normal", "normal"),
            thermal_states=("nominal", "serious", "fair", "nominal", "fair", "nominal"),
        ),
        budget=DERIVED_BUDGET,
    )
    assert "memory_pressure_unsafe" in gate.reasons
    assert "thermal_unsafe" in gate.reasons


def test_missing_resource_states_fail_closed() -> None:
    gate = evaluate_performance(
        passing_measurements(memory_pressure=(), thermal_states=(), peak_rss_bytes=None),
        budget=DERIVED_BUDGET,
    )
    assert "memory_pressure_missing" in gate.reasons
    assert "thermal_missing" in gate.reasons
    assert "peak_rss_missing" in gate.reasons


def test_peak_rss_above_the_ceiling_fails() -> None:
    gate = evaluate_performance(
        passing_measurements(peak_rss_bytes=DEFAULT_COMBINED_PEAK_RSS_LIMIT_BYTES + 1),
        budget=DERIVED_BUDGET,
    )
    assert "peak_rss_exceeded" in gate.reasons


def test_oom_is_always_a_failure() -> None:
    gate = evaluate_performance(passing_measurements(oom_events=1), budget=DERIVED_BUDGET)
    assert "oom_observed" in gate.reasons


def test_accumulating_resources_fail() -> None:
    gate = evaluate_performance(
        passing_measurements(
            rss_samples=(1, 2, 3, 4, 5, 6),
            swap_used_bytes=(0, 0, 0, 0, 1024, 4096),
            queue_depth=(0, 0, 1, 2, 3, 4),
        ),
        budget=DERIVED_BUDGET,
    )
    assert "rss_accumulating" in gate.reasons
    assert "swap_accumulating" in gate.reasons
    assert "queue_accumulating" in gate.reasons


def test_growth_detection_ignores_noise_but_catches_a_step_up() -> None:
    assert not detect_growth(STEADY).accumulating
    stepped = detect_growth((100, 100, 100, 100, 200, 200, 200, 200))
    assert stepped.accumulating
    assert stepped.growth_ratio is not None
    assert not stepped.strictly_increasing


def test_growth_detection_needs_enough_samples() -> None:
    check = detect_growth((1, 2, 3))
    assert check.samples == 3
    assert not check.accumulating
    assert check.growth_ratio is None


def test_growth_from_zero_baseline_is_still_growth() -> None:
    assert detect_growth((0, 0, 0, 0, 5, 5)).accumulating


def test_nearest_rank_percentiles() -> None:
    values = tuple(float(item) for item in range(1, 101))
    assert nearest_rank(values, 0.50) == 50.0
    assert nearest_rank(values, 0.95) == 95.0
    assert nearest_rank((), 0.95) is None


def test_measurements_reject_inconsistent_series() -> None:
    with pytest.raises(ValueError, match="per-turn series"):
        LocalLLMMeasurements(
            turns=2,
            prompt_processing_ms=(1.0,),
            first_token_ms=(1.0, 2.0),
            first_sentence_ms=(1.0, 2.0),
            output_tokens_per_second=(1.0, 2.0),
        )


def test_measurements_reject_unknown_states() -> None:
    with pytest.raises(ValueError, match="thermal state"):
        LocalLLMMeasurements(
            turns=0,
            prompt_processing_ms=(),
            first_token_ms=(),
            first_sentence_ms=(),
            output_tokens_per_second=(),
            thermal_states=("meltdown",),  # type: ignore[arg-type]
        )


def test_measurements_reject_negative_latency() -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        LocalLLMMeasurements(
            turns=1,
            prompt_processing_ms=(-1.0,),
            first_token_ms=(1.0,),
            first_sentence_ms=(1.0,),
            output_tokens_per_second=(1.0,),
        )


def test_ceiling_needs_no_upstream_measurement() -> None:
    """The ceiling is decisive on its own, unlike the derived budget."""

    budget = LatencyBudget()
    assert not budget.derived
    assert budget.first_sentence_budget_ms() is None
    assert budget.first_sentence_ceiling_ms() == pytest.approx(1_150.0)


def test_first_sentence_above_the_ceiling_fails_even_without_a_derived_budget() -> None:
    slow = passing_measurements(
        first_sentence_ms=tuple(1_200.0 for _ in range(MIN_STABILITY_TURNS))
    )
    gate = evaluate_performance(slow)
    assert "first_sentence_exceeds_full_budget" in gate.reasons
    assert not gate.passed


def test_first_sentence_below_the_ceiling_does_not_trip_it() -> None:
    gate = evaluate_performance(passing_measurements(), budget=DERIVED_BUDGET)
    assert "first_sentence_exceeds_full_budget" not in gate.reasons
    assert gate.passed, gate.reasons


def test_ceiling_is_reported_in_the_aggregates() -> None:
    gate = evaluate_performance(passing_measurements(), budget=DERIVED_BUDGET)
    assert gate.aggregates is not None
    assert gate.aggregates.first_sentence_ceiling_ms == pytest.approx(1_150.0)
