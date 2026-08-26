"""Numeric-only GPT-SoVITS spike measurements and acceptance gates."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Literal

ThermalState = Literal["nominal", "fair", "serious", "critical", "unknown"]
PerformanceReason = Literal[
    "corpus_incomplete",
    "ttfa_exceeded",
    "rtf_exceeded",
    "rss_missing",
    "rss_exceeded",
    "thermal_missing",
    "thermal_unsafe",
    "cancel_samples_missing",
    "cancel_deadline_exceeded",
]

TTFA_P95_LIMIT_MS: Final[float] = 1_000.0
RTF_P95_LIMIT: Final[float] = 0.8
PEAK_RSS_LIMIT_BYTES: Final[int] = 6 * 1024**3
CANCEL_LIMIT_MS: Final[float] = 500.0


@dataclass(frozen=True, slots=True)
class SpikeMeasurements:
    zh_samples: int
    en_samples: int
    mixed_samples: int
    ttfa_ms: tuple[float, ...]
    rtf: tuple[float, ...]
    peak_rss_bytes: int | None
    thermal_states: tuple[ThermalState, ...]
    cancel_stop_ms: tuple[float, ...]

    def __post_init__(self) -> None:
        counts = (self.zh_samples, self.en_samples, self.mixed_samples)
        if any(count < 0 for count in counts):
            raise ValueError("sample counts cannot be negative")
        sample_count = sum(counts)
        if len(self.ttfa_ms) != sample_count or len(self.rtf) != sample_count:
            raise ValueError("TTFA and RTF counts must equal corpus sample count")
        numeric_values = (*self.ttfa_ms, *self.rtf, *self.cancel_stop_ms)
        if any(not math.isfinite(value) or value < 0 for value in numeric_values):
            raise ValueError("measurements must be finite and non-negative")
        if self.peak_rss_bytes is not None and self.peak_rss_bytes < 0:
            raise ValueError("peak RSS cannot be negative")
        allowed_states: set[str] = {"nominal", "fair", "serious", "critical", "unknown"}
        if any(state not in allowed_states for state in self.thermal_states):
            raise ValueError("unknown thermal state")

    @property
    def sample_count(self) -> int:
        return self.zh_samples + self.en_samples + self.mixed_samples


@dataclass(frozen=True, slots=True)
class SanitizedAggregates:
    sample_count: int
    zh_samples: int
    en_samples: int
    mixed_samples: int
    ttfa_p95_ms: float | None
    rtf_p95: float | None
    peak_rss_bytes: int | None
    worst_cancel_ms: float | None
    worst_thermal_state: ThermalState | None


@dataclass(frozen=True, slots=True)
class PerformanceGate:
    evaluated: bool
    passed: bool
    reasons: tuple[PerformanceReason, ...]
    aggregates: SanitizedAggregates | None


def _nearest_rank_p95(values: tuple[float, ...]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return ordered[rank - 1]


def _worst_thermal_state(states: tuple[ThermalState, ...]) -> ThermalState | None:
    if not states:
        return None
    order: dict[ThermalState, int] = {
        "nominal": 0,
        "fair": 1,
        "unknown": 2,
        "serious": 3,
        "critical": 4,
    }
    return max(states, key=order.__getitem__)


def evaluate_performance(measurements: SpikeMeasurements | None) -> PerformanceGate:
    if measurements is None:
        return PerformanceGate(evaluated=False, passed=False, reasons=(), aggregates=None)

    ttfa_p95 = _nearest_rank_p95(measurements.ttfa_ms)
    rtf_p95 = _nearest_rank_p95(measurements.rtf)
    worst_cancel = max(measurements.cancel_stop_ms, default=None)
    worst_thermal = _worst_thermal_state(measurements.thermal_states)
    aggregates = SanitizedAggregates(
        sample_count=measurements.sample_count,
        zh_samples=measurements.zh_samples,
        en_samples=measurements.en_samples,
        mixed_samples=measurements.mixed_samples,
        ttfa_p95_ms=ttfa_p95,
        rtf_p95=rtf_p95,
        peak_rss_bytes=measurements.peak_rss_bytes,
        worst_cancel_ms=worst_cancel,
        worst_thermal_state=worst_thermal,
    )

    reasons: list[PerformanceReason] = []
    if min(measurements.zh_samples, measurements.en_samples, measurements.mixed_samples) < 1:
        reasons.append("corpus_incomplete")
    if ttfa_p95 is None or ttfa_p95 > TTFA_P95_LIMIT_MS:
        reasons.append("ttfa_exceeded")
    if rtf_p95 is None or rtf_p95 > RTF_P95_LIMIT:
        reasons.append("rtf_exceeded")
    if measurements.peak_rss_bytes is None:
        reasons.append("rss_missing")
    elif measurements.peak_rss_bytes > PEAK_RSS_LIMIT_BYTES:
        reasons.append("rss_exceeded")
    if worst_thermal is None:
        reasons.append("thermal_missing")
    elif worst_thermal not in {"nominal", "fair"}:
        reasons.append("thermal_unsafe")
    if worst_cancel is None:
        reasons.append("cancel_samples_missing")
    elif worst_cancel > CANCEL_LIMIT_MS:
        reasons.append("cancel_deadline_exceeded")

    return PerformanceGate(
        evaluated=True,
        passed=not reasons,
        reasons=tuple(reasons),
        aggregates=aggregates,
    )
