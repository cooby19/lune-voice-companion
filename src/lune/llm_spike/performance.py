"""Numeric-only local LLM spike measurements and acceptance gates.

Nothing here invents a standalone latency target. The end-to-end budget is the one the
plan already fixes (p50 <=1.5 s, p95 <=2.2 s from the last voiced sample to the first
non-silent output frame); the share left for the model is derived by subtracting the fixed
end-of-speech silence and the measured Whisper and TTS costs. Those upstream numbers come
from the M2 and M5 local gates, which have not been run, so the derivation reports
"underived" rather than guessing - and an underived budget fails the gate instead of
quietly passing it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise
from typing import Final, Literal

type MemoryPressure = Literal["normal", "warn", "critical", "unknown"]
type ThermalState = Literal["nominal", "fair", "serious", "critical", "unknown"]
type PerformanceReason = Literal[
    "turns_insufficient",
    "samples_missing",
    "cold_start_missing",
    "warm_start_missing",
    "first_sentence_budget_underived",
    "first_sentence_budget_exceeded",
    "first_sentence_exceeds_full_budget",
    "peak_rss_missing",
    "peak_rss_exceeded",
    "memory_pressure_missing",
    "memory_pressure_unsafe",
    "thermal_missing",
    "thermal_unsafe",
    "oom_observed",
    "rss_accumulating",
    "swap_accumulating",
    "queue_accumulating",
]

MIN_STABILITY_TURNS: Final[int] = 30
END_TO_END_P50_LIMIT_MS: Final[float] = 1_500.0
END_TO_END_P95_LIMIT_MS: Final[float] = 2_200.0
END_SILENCE_MS: Final[float] = 350.0
GROWTH_TOLERANCE_RATIO: Final[float] = 0.25
MIN_GROWTH_SAMPLES: Final[int] = 6

DEFAULT_COMBINED_PEAK_RSS_LIMIT_BYTES: Final[int] = 10 * 1024**3
"""Default ceiling for engine plus workers on the 16 GiB target machine.

Six GiB is held back for macOS, the menu-bar app and page cache so the combination of
Whisper, E5, the release TTS backend and the model does not push the machine into swap.
This is a spike default, not a measured constant; the user may set another ceiling.
"""

_PRESSURE_ORDER: Final[dict[str, int]] = {"normal": 0, "unknown": 1, "warn": 2, "critical": 3}
_THERMAL_ORDER: Final[dict[str, int]] = {
    "nominal": 0,
    "fair": 1,
    "unknown": 2,
    "serious": 3,
    "critical": 4,
}


@dataclass(frozen=True, slots=True)
class LatencyBudget:
    """Split the fixed end-to-end budget into the share available to the model."""

    end_to_end_p50_ms: float = END_TO_END_P50_LIMIT_MS
    end_to_end_p95_ms: float = END_TO_END_P95_LIMIT_MS
    end_silence_ms: float = END_SILENCE_MS
    stt_final_p50_ms: float | None = None
    tts_ttfa_p50_ms: float | None = None

    def __post_init__(self) -> None:
        values = (self.end_to_end_p50_ms, self.end_to_end_p95_ms, self.end_silence_ms)
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("end-to-end budget values must be finite and positive")
        for optional in (self.stt_final_p50_ms, self.tts_ttfa_p50_ms):
            if optional is not None and (not math.isfinite(optional) or optional < 0):
                raise ValueError("upstream latency inputs must be finite and non-negative")

    @property
    def derived(self) -> bool:
        return self.stt_final_p50_ms is not None and self.tts_ttfa_p50_ms is not None

    def first_sentence_ceiling_ms(self) -> float:
        """The most the model could ever have, even if Whisper and TTS were instant.

        Unlike the derived budget this needs no upstream measurement, so exceeding it is
        decisive on its own: no improvement elsewhere in the pipeline could rescue it.
        """

        return max(self.end_to_end_p50_ms - self.end_silence_ms, 0.0)

    def first_sentence_budget_ms(self) -> float | None:
        """Milliseconds left for the model's first complete sentence, or None if unknown.

        `SentenceGate` releases whole sentences, so the end-to-end clock depends on the
        first sentence rather than the first token.
        """

        if self.stt_final_p50_ms is None or self.tts_ttfa_p50_ms is None:
            return None
        remaining = (
            self.end_to_end_p50_ms
            - self.end_silence_ms
            - self.stt_final_p50_ms
            - self.tts_ttfa_p50_ms
        )
        return max(remaining, 0.0)


@dataclass(frozen=True, slots=True)
class GrowthCheck:
    """Whether a resource series accumulates across the run."""

    samples: int
    strictly_increasing: bool
    first_window_mean: float | None
    last_window_mean: float | None
    growth_ratio: float | None
    accumulating: bool


@dataclass(frozen=True, slots=True)
class LocalLLMMeasurements:
    """Raw spike numbers. No prompt, transcript, persona or path may appear here."""

    turns: int
    prompt_processing_ms: tuple[float, ...]
    first_token_ms: tuple[float, ...]
    first_sentence_ms: tuple[float, ...]
    output_tokens_per_second: tuple[float, ...]
    cold_start_ms: float | None = None
    warm_start_ms: float | None = None
    peak_rss_bytes: int | None = None
    rss_samples: tuple[int, ...] = ()
    swap_used_bytes: tuple[int, ...] = ()
    queue_depth: tuple[int, ...] = ()
    memory_pressure: tuple[MemoryPressure, ...] = ()
    thermal_states: tuple[ThermalState, ...] = ()
    oom_events: int = 0

    def __post_init__(self) -> None:
        if self.turns < 0:
            raise ValueError("turn count cannot be negative")
        if self.oom_events < 0:
            raise ValueError("OOM event count cannot be negative")
        per_turn = (
            self.prompt_processing_ms,
            self.first_token_ms,
            self.first_sentence_ms,
            self.output_tokens_per_second,
        )
        if any(len(series) != self.turns for series in per_turn):
            raise ValueError("per-turn series lengths must equal the turn count")
        floats = (*self.prompt_processing_ms, *self.first_token_ms, *self.first_sentence_ms)
        if any(not math.isfinite(value) or value < 0 for value in floats):
            raise ValueError("latency samples must be finite and non-negative")
        if any(not math.isfinite(value) or value < 0 for value in self.output_tokens_per_second):
            raise ValueError("throughput samples must be finite and non-negative")
        for optional in (self.cold_start_ms, self.warm_start_ms):
            if optional is not None and (not math.isfinite(optional) or optional < 0):
                raise ValueError("start-up samples must be finite and non-negative")
        counters = (*self.rss_samples, *self.swap_used_bytes, *self.queue_depth)
        if any(value < 0 for value in counters):
            raise ValueError("resource samples cannot be negative")
        if self.peak_rss_bytes is not None and self.peak_rss_bytes < 0:
            raise ValueError("peak RSS cannot be negative")
        if any(state not in _PRESSURE_ORDER for state in self.memory_pressure):
            raise ValueError("unknown memory pressure state")
        if any(state not in _THERMAL_ORDER for state in self.thermal_states):
            raise ValueError("unknown thermal state")


@dataclass(frozen=True, slots=True)
class LocalLLMAggregates:
    turns: int
    cold_start_ms: float | None
    warm_start_ms: float | None
    prompt_processing_p95_ms: float | None
    first_token_p50_ms: float | None
    first_token_p95_ms: float | None
    first_sentence_p50_ms: float | None
    first_sentence_p95_ms: float | None
    output_tokens_per_second_p50: float | None
    first_sentence_budget_ms: float | None
    first_sentence_ceiling_ms: float
    peak_rss_bytes: int | None
    peak_swap_bytes: int | None
    worst_memory_pressure: MemoryPressure | None
    worst_thermal_state: ThermalState | None
    rss_growth: GrowthCheck
    swap_growth: GrowthCheck
    queue_growth: GrowthCheck


@dataclass(frozen=True, slots=True)
class PerformanceGate:
    evaluated: bool
    passed: bool
    reasons: tuple[PerformanceReason, ...]
    aggregates: LocalLLMAggregates | None


def nearest_rank(values: tuple[float, ...], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(quantile * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def detect_growth(
    values: tuple[int, ...] | tuple[float, ...],
    *,
    tolerance_ratio: float = GROWTH_TOLERANCE_RATIO,
) -> GrowthCheck:
    """Flag a series that climbs monotonically or ends materially above where it started."""

    count = len(values)
    if count < MIN_GROWTH_SAMPLES:
        return GrowthCheck(
            samples=count,
            strictly_increasing=False,
            first_window_mean=None,
            last_window_mean=None,
            growth_ratio=None,
            accumulating=False,
        )
    window = max(1, count // 3)
    first_mean = sum(values[:window]) / window
    last_mean = sum(values[-window:]) / window
    strictly_increasing = all(after > before for before, after in pairwise(values))
    growth_ratio: float | None = None
    if first_mean > 0:
        ratio = (last_mean - first_mean) / first_mean
        growth_ratio = ratio
        accumulating = strictly_increasing or ratio > tolerance_ratio
    else:
        accumulating = strictly_increasing or last_mean > 0
    return GrowthCheck(
        samples=count,
        strictly_increasing=strictly_increasing,
        first_window_mean=first_mean,
        last_window_mean=last_mean,
        growth_ratio=growth_ratio,
        accumulating=accumulating,
    )


def _worst(states: tuple[str, ...], order: dict[str, int]) -> str | None:
    if not states:
        return None
    return max(states, key=order.__getitem__)


def evaluate_performance(
    measurements: LocalLLMMeasurements | None,
    *,
    budget: LatencyBudget | None = None,
    peak_rss_limit_bytes: int = DEFAULT_COMBINED_PEAK_RSS_LIMIT_BYTES,
) -> PerformanceGate:
    """Grade one spike run, treating every missing input as a failure rather than a pass."""

    if measurements is None:
        return PerformanceGate(evaluated=False, passed=False, reasons=(), aggregates=None)

    effective_budget = budget or LatencyBudget()
    first_sentence_budget = effective_budget.first_sentence_budget_ms()
    first_sentence_p50 = nearest_rank(measurements.first_sentence_ms, 0.50)
    worst_pressure = _worst(tuple(measurements.memory_pressure), _PRESSURE_ORDER)
    worst_thermal = _worst(tuple(measurements.thermal_states), _THERMAL_ORDER)
    rss_growth = detect_growth(measurements.rss_samples)
    swap_growth = detect_growth(measurements.swap_used_bytes)
    queue_growth = detect_growth(measurements.queue_depth)

    aggregates = LocalLLMAggregates(
        turns=measurements.turns,
        cold_start_ms=measurements.cold_start_ms,
        warm_start_ms=measurements.warm_start_ms,
        prompt_processing_p95_ms=nearest_rank(measurements.prompt_processing_ms, 0.95),
        first_token_p50_ms=nearest_rank(measurements.first_token_ms, 0.50),
        first_token_p95_ms=nearest_rank(measurements.first_token_ms, 0.95),
        first_sentence_p50_ms=first_sentence_p50,
        first_sentence_p95_ms=nearest_rank(measurements.first_sentence_ms, 0.95),
        output_tokens_per_second_p50=nearest_rank(measurements.output_tokens_per_second, 0.50),
        first_sentence_budget_ms=first_sentence_budget,
        first_sentence_ceiling_ms=effective_budget.first_sentence_ceiling_ms(),
        peak_rss_bytes=measurements.peak_rss_bytes,
        peak_swap_bytes=max(measurements.swap_used_bytes, default=None),
        worst_memory_pressure=_cast_pressure(worst_pressure),
        worst_thermal_state=_cast_thermal(worst_thermal),
        rss_growth=rss_growth,
        swap_growth=swap_growth,
        queue_growth=queue_growth,
    )

    reasons: list[PerformanceReason] = []
    if measurements.turns < MIN_STABILITY_TURNS:
        reasons.append("turns_insufficient")
    if first_sentence_p50 is None:
        reasons.append("samples_missing")
    if measurements.cold_start_ms is None:
        reasons.append("cold_start_missing")
    if measurements.warm_start_ms is None:
        reasons.append("warm_start_missing")
    if first_sentence_budget is None:
        reasons.append("first_sentence_budget_underived")
    elif first_sentence_p50 is not None and first_sentence_p50 > first_sentence_budget:
        reasons.append("first_sentence_budget_exceeded")
    if (
        first_sentence_p50 is not None
        and first_sentence_p50 > effective_budget.first_sentence_ceiling_ms()
    ):
        reasons.append("first_sentence_exceeds_full_budget")
    if measurements.peak_rss_bytes is None:
        reasons.append("peak_rss_missing")
    elif measurements.peak_rss_bytes > peak_rss_limit_bytes:
        reasons.append("peak_rss_exceeded")
    if worst_pressure is None:
        reasons.append("memory_pressure_missing")
    elif worst_pressure != "normal":
        reasons.append("memory_pressure_unsafe")
    if worst_thermal is None:
        reasons.append("thermal_missing")
    elif worst_thermal not in {"nominal", "fair"}:
        reasons.append("thermal_unsafe")
    if measurements.oom_events:
        reasons.append("oom_observed")
    if rss_growth.accumulating:
        reasons.append("rss_accumulating")
    if swap_growth.accumulating:
        reasons.append("swap_accumulating")
    if queue_growth.accumulating:
        reasons.append("queue_accumulating")

    return PerformanceGate(
        evaluated=True,
        passed=not reasons,
        reasons=tuple(reasons),
        aggregates=aggregates,
    )


def _cast_pressure(value: str | None) -> MemoryPressure | None:
    if value is None:
        return None
    assert value in _PRESSURE_ORDER
    return value  # type: ignore[return-value]


def _cast_thermal(value: str | None) -> ThermalState | None:
    if value is None:
        return None
    assert value in _THERMAL_ORDER
    return value  # type: ignore[return-value]
