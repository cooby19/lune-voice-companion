from __future__ import annotations

import pytest

from lune.llm_spike.cancellation import (
    CANCEL_STOP_LIMIT_MS,
    CancelObservation,
    evaluate_cancellation,
)


def clean(proof: str = "inference_stopped", stop_ms: float = 80.0) -> CancelObservation:
    return CancelObservation(stop_latency_ms=stop_ms, proof=proof)  # type: ignore[arg-type]


def test_no_trials_is_not_a_pass() -> None:
    gate = evaluate_cancellation(())
    assert not gate.evaluated
    assert not gate.passed
    assert not gate.declared_remote_cancel


def test_clean_cancellation_passes_and_declares_remote_cancel() -> None:
    gate = evaluate_cancellation((clean(), clean(), clean()))
    assert gate.passed
    assert gate.declared_remote_cancel
    assert gate.worst_stop_latency_ms == pytest.approx(80.0)


def test_client_side_close_still_passes_but_cannot_claim_remote_cancel() -> None:
    gate = evaluate_cancellation((clean(), clean("client_stream_closed_only")))
    assert gate.passed
    assert not gate.declared_remote_cancel


def test_unknown_proof_never_declares_remote_cancel() -> None:
    gate = evaluate_cancellation((clean("unknown"),))
    assert gate.passed
    assert not gate.declared_remote_cancel


def test_late_events_after_cancel_always_fail() -> None:
    gate = evaluate_cancellation(
        (
            CancelObservation(stop_latency_ms=50.0, proof="inference_stopped", late_text_events=1),
            CancelObservation(stop_latency_ms=50.0, proof="inference_stopped", late_tool_calls=2),
            CancelObservation(stop_latency_ms=50.0, proof="inference_stopped", late_pcm_chunks=3),
        )
    )
    assert not gate.passed
    assert set(gate.reasons) == {
        "late_text_after_cancel",
        "late_tool_call_after_cancel",
        "late_pcm_after_cancel",
    }
    assert gate.late_text_events == 1
    assert gate.late_tool_calls == 2
    assert gate.late_pcm_chunks == 3


def test_slow_stop_fails_the_deadline() -> None:
    gate = evaluate_cancellation((clean(stop_ms=CANCEL_STOP_LIMIT_MS + 1),))
    assert not gate.passed
    assert "stop_deadline_exceeded" in gate.reasons


def test_stop_exactly_at_the_deadline_passes() -> None:
    assert evaluate_cancellation((clean(stop_ms=CANCEL_STOP_LIMIT_MS),)).passed


def test_negative_measurements_are_rejected() -> None:
    with pytest.raises(ValueError, match="stop latency"):
        CancelObservation(stop_latency_ms=-1.0, proof="unknown")
    with pytest.raises(ValueError, match="late event"):
        CancelObservation(stop_latency_ms=1.0, proof="unknown", late_text_events=-1)
