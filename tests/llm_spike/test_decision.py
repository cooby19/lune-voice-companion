from __future__ import annotations

from pathlib import Path

from lune.llm_spike.cancellation import CancelObservation, evaluate_cancellation
from lune.llm_spike.decision import LocalProviderDecision, decide_local_provider
from lune.llm_spike.model_pin import LocalLLMManifestCheck
from lune.llm_spike.performance import PerformanceGate
from lune.llm_spike.runtime import RuntimeProbe
from lune.llm_spike.thinking import ThinkingFilterResult, ThinkingGate, evaluate_thinking
from lune.llm_spike.tools import ToolCallGate
from lune.stt.model_manifest import VerifiedModelManifest


def ready_manifest() -> LocalLLMManifestCheck:
    return LocalLLMManifestCheck(
        reason="ready",
        manifest=VerifiedModelManifest(
            model_id="Qwen/Qwen3.5-4B",
            revision="b" * 40,
            model_root=Path("/private/model"),
            files=(),
        ),
    )


def passing_thinking() -> ThinkingGate:
    return evaluate_thinking((ThinkingFilterResult(text="好。", violations=()),))


def passing_tools() -> ToolCallGate:
    return ToolCallGate(
        evaluated=True,
        passed=True,
        reasons=(),
        turns_observed=30,
        accepted_calls=30,
        rejected_calls=0,
        malformed_ratio=0.0,
    )


def passing_performance() -> PerformanceGate:
    return PerformanceGate(evaluated=True, passed=True, reasons=(), aggregates=None)


def one_usable_runtime() -> tuple[RuntimeProbe, ...]:
    return (
        RuntimeProbe(name="mlx_lm_worker", status="installed"),
        RuntimeProbe(name="ollama", status="not_authorised"),
    )


def decide(**overrides: object) -> LocalProviderDecision:
    kwargs: dict[str, object] = {
        "runtime_probes": one_usable_runtime(),
        "manifest_check": ready_manifest(),
        "thinking_gate": passing_thinking(),
        "tool_gate": passing_tools(),
        "cancellation_gate": evaluate_cancellation(
            (CancelObservation(stop_latency_ms=60.0, proof="inference_stopped"),)
        ),
        "performance_gate": passing_performance(),
    }
    kwargs.update(overrides)
    return decide_local_provider(**kwargs)  # type: ignore[arg-type]


def test_everything_green_selects_the_local_provider() -> None:
    decision = decide()
    assert decision.provider == "local_qwen"
    assert decision.local_enabled
    assert decision.selected_runtime == "mlx_lm_worker"
    assert decision.declared_remote_cancel
    assert decision.reasons == ("local_qwen_eligible",)


def test_todays_state_keeps_openai_and_lists_every_missing_gate() -> None:
    decision = decide_local_provider(
        runtime_probes=(RuntimeProbe(name="ollama", status="not_authorised"),),
        manifest_check=LocalLLMManifestCheck(reason="pin_not_established"),
        thinking_gate=evaluate_thinking(()),
        tool_gate=ToolCallGate(
            evaluated=False,
            passed=False,
            reasons=("no_observations",),
            turns_observed=0,
            accepted_calls=0,
            rejected_calls=0,
            malformed_ratio=None,
        ),
        cancellation_gate=evaluate_cancellation(()),
        performance_gate=PerformanceGate(
            evaluated=False, passed=False, reasons=(), aggregates=None
        ),
    )
    assert decision.provider == "openai_responses"
    assert not decision.local_enabled
    assert decision.selected_runtime is None
    assert not decision.declared_remote_cancel
    assert set(decision.reasons) == {
        "runtime_not_selected",
        "model_pin_not_established",
        "thinking_not_run",
        "tool_calls_not_run",
        "cancellation_not_run",
        "performance_not_run",
    }


def test_ambiguous_runtime_selection_is_refused() -> None:
    decision = decide(
        runtime_probes=(
            RuntimeProbe(name="mlx_lm_worker", status="installed"),
            RuntimeProbe(name="llama_cpp_server", status="installed"),
        )
    )
    assert decision.provider == "openai_responses"
    assert "runtime_not_selected" in decision.reasons


def test_a_single_failing_gate_blocks_the_local_provider() -> None:
    failing = PerformanceGate(
        evaluated=True, passed=False, reasons=("peak_rss_exceeded",), aggregates=None
    )
    decision = decide(performance_gate=failing)
    assert decision.provider == "openai_responses"
    assert decision.reasons == ("performance_gate_failed",)


def test_leaked_reasoning_blocks_the_local_provider() -> None:
    dirty = evaluate_thinking(
        (ThinkingFilterResult(text="好。", violations=("reasoning_emitted",)),)
    )
    decision = decide(thinking_gate=dirty)
    assert not decision.local_enabled
    assert "thinking_gate_failed" in decision.reasons


def test_unproven_cancellation_still_disables_the_remote_cancel_claim() -> None:
    decision = decide(
        cancellation_gate=evaluate_cancellation(
            (CancelObservation(stop_latency_ms=60.0, proof="client_stream_closed_only"),)
        )
    )
    assert decision.provider == "local_qwen"
    assert not decision.declared_remote_cancel


def test_unverified_model_files_block_the_local_provider() -> None:
    decision = decide(manifest_check=LocalLLMManifestCheck(reason="file_checksum_mismatch"))
    assert not decision.local_enabled
    assert "model_manifest_unavailable" in decision.reasons
