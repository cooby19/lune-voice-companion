"""Gate arithmetic for the M6 end-to-end latency and interruption budgets."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from lune.pipeline.coordinator import GenerationCoordinator
from lune.pipeline.playback import PlaybackSink
from lune.pipeline.session import VoiceSession

REQUIRED_WARM_TURNS = 30
P50_BUDGET_MS = 1_500.0
P95_BUDGET_MS = 2_200.0
INTERRUPTION_BUDGET_MS = 200.0

type EndToEndReason = Literal[
    "no_samples",
    "turns_insufficient",
    "undelivered_turns",
    "p50_exceeded",
    "p95_exceeded",
]
type InterruptionReason = Literal[
    "no_samples",
    "stop_exceeded",
    "late_events",
]


@dataclass(frozen=True, slots=True)
class TurnLatencySample:
    """One warm turn: last voiced input sample to first non-silent output frame."""

    generation_id: int
    speech_end_at: float
    first_audible_at: float | None = None

    def __post_init__(self) -> None:
        if self.first_audible_at is not None and self.first_audible_at < self.speech_end_at:
            raise ValueError("output cannot precede the end of the input speech")

    @property
    def delivered(self) -> bool:
        return self.first_audible_at is not None

    @property
    def latency_ms(self) -> float | None:
        if self.first_audible_at is None:
            return None
        return (self.first_audible_at - self.speech_end_at) * 1_000.0


@dataclass(frozen=True, slots=True)
class InterruptionSample:
    """One confirmed barge-in: how long audible output survived the fence."""

    generation_id: int
    audible_stop_ms: float
    late_events: int = 0

    def __post_init__(self) -> None:
        if self.audible_stop_ms < 0 or self.late_events < 0:
            raise ValueError("interruption measurements cannot be negative")


@dataclass(frozen=True, slots=True)
class EndToEndGate:
    evaluated: bool
    passed: bool
    reasons: tuple[EndToEndReason, ...]
    turns: int
    delivered: int
    p50_ms: float | None
    p95_ms: float | None
    max_ms: float | None


@dataclass(frozen=True, slots=True)
class InterruptionGate:
    evaluated: bool
    passed: bool
    reasons: tuple[InterruptionReason, ...]
    trials: int
    p95_ms: float | None
    max_ms: float | None
    late_events: int


def evaluate_end_to_end(
    samples: Sequence[TurnLatencySample],
    *,
    required_turns: int = REQUIRED_WARM_TURNS,
    p50_budget_ms: float = P50_BUDGET_MS,
    p95_budget_ms: float = P95_BUDGET_MS,
) -> EndToEndGate:
    """Grade warm turns; missing evidence fails rather than silently passing."""

    if required_turns < 1:
        raise ValueError("the gate needs at least one required turn")
    if not samples:
        return EndToEndGate(False, False, ("no_samples",), 0, 0, None, None, None)

    reasons: list[EndToEndReason] = []
    latencies = sorted(sample.latency_ms for sample in samples if sample.latency_ms is not None)
    delivered = len(latencies)
    if len(samples) < required_turns:
        reasons.append("turns_insufficient")
    if delivered < len(samples):
        reasons.append("undelivered_turns")
    p50 = _percentile(latencies, 0.50)
    p95 = _percentile(latencies, 0.95)
    if p50 is not None and p50 > p50_budget_ms:
        reasons.append("p50_exceeded")
    if p95 is not None and p95 > p95_budget_ms:
        reasons.append("p95_exceeded")
    return EndToEndGate(
        evaluated=True,
        passed=not reasons,
        reasons=tuple(reasons),
        turns=len(samples),
        delivered=delivered,
        p50_ms=p50,
        p95_ms=p95,
        max_ms=latencies[-1] if latencies else None,
    )


def evaluate_interruption(
    samples: Sequence[InterruptionSample],
    *,
    budget_ms: float = INTERRUPTION_BUDGET_MS,
) -> InterruptionGate:
    """Every barge-in must silence output inside the budget and leak nothing after."""

    if not samples:
        return InterruptionGate(False, False, ("no_samples",), 0, None, None, 0)
    reasons: list[InterruptionReason] = []
    stops = sorted(sample.audible_stop_ms for sample in samples)
    late = sum(sample.late_events for sample in samples)
    if stops[-1] > budget_ms:
        reasons.append("stop_exceeded")
    if late:
        reasons.append("late_events")
    return InterruptionGate(
        evaluated=True,
        passed=not reasons,
        reasons=tuple(reasons),
        trials=len(samples),
        p95_ms=_percentile(stops, 0.95),
        max_ms=stops[-1],
        late_events=late,
    )


def collect_latency_samples(
    session: VoiceSession,
    playback: PlaybackSink,
) -> tuple[TurnLatencySample, ...]:
    """Pair each reported turn with the clock the playback sink actually stamped."""

    samples: list[TurnLatencySample] = []
    for report in session.reports:
        speech_end = session.speech_end_at(report.generation_id)
        if speech_end is None:
            continue
        samples.append(
            TurnLatencySample(
                generation_id=report.generation_id,
                speech_end_at=speech_end,
                first_audible_at=playback.first_audible_at(report.generation_id),
            )
        )
    return tuple(samples)


def collect_interruption_samples(
    coordinator: GenerationCoordinator,
    *,
    late_events: Mapping[int, int] | None = None,
) -> tuple[InterruptionSample, ...]:
    """Turn every confirmed barge-in into a gradable sample.

    ``late_events`` counts anything that still reached the user after the fence
    closed, keyed by the cancelled generation; the caller measures it because
    only the caller knows which sinks it was watching.
    """

    counts = late_events or {}
    return tuple(
        InterruptionSample(
            generation_id=event.previous_generation_id,
            audible_stop_ms=event.audible_stop_ms,
            late_events=counts.get(event.previous_generation_id, 0),
        )
        for event in coordinator.cancel_events
        if event.reason == "barge_in"
    )


def _percentile(sorted_values: Sequence[float], quantile: float) -> float | None:
    """Nearest-rank percentile: no interpolation invents a value never measured."""

    if not sorted_values:
        return None
    rank = math.ceil(quantile * len(sorted_values))
    return sorted_values[max(0, min(len(sorted_values) - 1, rank - 1))]
