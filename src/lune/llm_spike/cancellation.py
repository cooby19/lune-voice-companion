"""Cancellation evidence for a local runtime, and honest capability reporting.

Two separate questions. First, did anything reach downstream after the fence closed? That
is a hard failure whatever the backend can do. Second, did the runtime actually stop
computing, or did the client merely stop reading? Only the first answer decides the gate;
only proven server-side stopping may be reported as `remote_cancel`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

CANCEL_STOP_LIMIT_MS: Final[float] = 200.0
"""Matches the plan's rule that audible output stops within 200 ms of a confirmed barge-in."""

type CancelProof = Literal[
    "inference_stopped",
    "client_stream_closed_only",
    "unknown",
]
type CancellationReason = Literal[
    "no_observations",
    "late_text_after_cancel",
    "late_tool_call_after_cancel",
    "late_pcm_after_cancel",
    "stop_deadline_exceeded",
]


@dataclass(frozen=True, slots=True)
class CancelObservation:
    """One cancellation trial.

    `stop_latency_ms` is the interval from the cancel request to the last event the runtime
    produced. The `late_*` counters are events that passed the generation fence afterwards
    and must always be zero.
    """

    stop_latency_ms: float
    proof: CancelProof
    late_text_events: int = 0
    late_tool_calls: int = 0
    late_pcm_chunks: int = 0

    def __post_init__(self) -> None:
        if self.stop_latency_ms < 0:
            raise ValueError("stop latency cannot be negative")
        counters = (self.late_text_events, self.late_tool_calls, self.late_pcm_chunks)
        if any(value < 0 for value in counters):
            raise ValueError("late event counts cannot be negative")


@dataclass(frozen=True, slots=True)
class CancellationGate:
    evaluated: bool
    passed: bool
    reasons: tuple[CancellationReason, ...]
    samples: int
    worst_stop_latency_ms: float | None
    late_text_events: int
    late_tool_calls: int
    late_pcm_chunks: int
    declared_remote_cancel: bool


def evaluate_cancellation(observations: tuple[CancelObservation, ...]) -> CancellationGate:
    """Grade cancellation and derive the `remote_cancel` capability from evidence only."""

    if not observations:
        return CancellationGate(
            evaluated=False,
            passed=False,
            reasons=("no_observations",),
            samples=0,
            worst_stop_latency_ms=None,
            late_text_events=0,
            late_tool_calls=0,
            late_pcm_chunks=0,
            declared_remote_cancel=False,
        )

    late_text = sum(item.late_text_events for item in observations)
    late_tools = sum(item.late_tool_calls for item in observations)
    late_pcm = sum(item.late_pcm_chunks for item in observations)
    worst_stop = max(item.stop_latency_ms for item in observations)

    reasons: list[CancellationReason] = []
    if late_text:
        reasons.append("late_text_after_cancel")
    if late_tools:
        reasons.append("late_tool_call_after_cancel")
    if late_pcm:
        reasons.append("late_pcm_after_cancel")
    if worst_stop > CANCEL_STOP_LIMIT_MS:
        reasons.append("stop_deadline_exceeded")

    return CancellationGate(
        evaluated=True,
        passed=not reasons,
        reasons=tuple(reasons),
        samples=len(observations),
        worst_stop_latency_ms=worst_stop,
        late_text_events=late_text,
        late_tool_calls=late_tools,
        late_pcm_chunks=late_pcm,
        declared_remote_cancel=all(item.proof == "inference_stopped" for item in observations),
    )
