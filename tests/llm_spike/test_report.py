from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from lune.llm_spike.cancellation import CancelObservation, evaluate_cancellation
from lune.llm_spike.decision import decide_local_provider
from lune.llm_spike.model_pin import LocalLLMManifestCheck
from lune.llm_spike.performance import (
    LatencyBudget,
    LocalLLMMeasurements,
    PerformanceGate,
    evaluate_performance,
)
from lune.llm_spike.report import (
    SanitizedLocalLLMReport,
    build_sanitized_report,
    write_sanitized_report,
)
from lune.llm_spike.runtime import RuntimeProbe
from lune.llm_spike.thinking import ThinkingFilterResult, evaluate_thinking
from lune.llm_spike.tools import ToolCallGate, ToolCallObservation, evaluate_tool_calls

PRIVATE = "使用者的私人記憶內容"


def measured() -> PerformanceGate:
    turns = 30
    measurements = LocalLLMMeasurements(
        turns=turns,
        prompt_processing_ms=tuple(80.0 for _ in range(turns)),
        first_token_ms=tuple(120.0 for _ in range(turns)),
        first_sentence_ms=tuple(400.0 for _ in range(turns)),
        output_tokens_per_second=tuple(45.0 for _ in range(turns)),
        cold_start_ms=2_500.0,
        warm_start_ms=300.0,
        peak_rss_bytes=7 * 1024**3,
        rss_samples=(100, 102, 99, 101, 100, 103),
        swap_used_bytes=(0, 0, 0, 0, 0, 0),
        queue_depth=(0, 1, 0, 1, 0, 0),
        memory_pressure=tuple("normal" for _ in range(6)),
        thermal_states=tuple("nominal" for _ in range(6)),
    )
    return evaluate_performance(
        measurements, budget=LatencyBudget(stt_final_p50_ms=300.0, tts_ttfa_p50_ms=400.0)
    )


def not_run_report() -> SanitizedLocalLLMReport:
    return build_sanitized_report(
        manifest_status="pin_not_established",
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
        decision=decide_local_provider(
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
        ),
    )


def test_unrun_spike_reports_not_run_and_keeps_openai() -> None:
    payload = not_run_report().to_dict()
    assert payload["provider"] == "openai_responses"
    assert payload["local_enabled"] is False
    assert payload["declared_remote_cancel"] is False
    assert payload["selected_runtime"] is None
    assert payload["metrics"] is None
    for key in (
        "thinking_status",
        "tool_call_status",
        "cancellation_status",
        "performance_status",
    ):
        assert payload[key] == "not_run"


def test_report_carries_numbers_but_never_private_text(tmp_path: Path) -> None:
    tool_gate = evaluate_tool_calls(
        tuple(
            ToolCallObservation(
                turn_index=index,
                tool_name="propose_memory",
                arguments_json=json.dumps(
                    {
                        "content": f"{PRIVATE} {index}",
                        "category": "stable_preference",
                        "importance": 0.5,
                    },
                    ensure_ascii=False,
                ),
            )
            for index in range(30)
        )
    )
    report = build_sanitized_report(
        manifest_status="ready",
        thinking_gate=evaluate_thinking((ThinkingFilterResult(text=PRIVATE, violations=()),)),
        tool_gate=tool_gate,
        cancellation_gate=evaluate_cancellation(
            (CancelObservation(stop_latency_ms=60.0, proof="inference_stopped"),)
        ),
        performance_gate=measured(),
        decision=decide_local_provider(
            runtime_probes=(RuntimeProbe(name="mlx_lm_worker", status="installed"),),
            manifest_check=LocalLLMManifestCheck(reason="pin_not_established"),
            thinking_gate=evaluate_thinking((ThinkingFilterResult(text="好。", violations=()),)),
            tool_gate=tool_gate,
            cancellation_gate=evaluate_cancellation(
                (CancelObservation(stop_latency_ms=60.0, proof="inference_stopped"),)
            ),
            performance_gate=measured(),
        ),
    )
    destination = tmp_path / "logs" / "local-llm-spike.json"
    write_sanitized_report(report, destination)

    raw = destination.read_text(encoding="utf-8")
    assert PRIVATE not in raw
    payload = json.loads(raw)
    assert payload["schema_version"] == 1
    assert payload["metrics"]["turns"] == 30
    assert payload["metrics"]["first_sentence_budget_ms"] == 450.0
    assert payload["metrics"]["rss_growth"]["accumulating"] is False
    assert payload["performance_status"] == "passed"


def test_report_file_is_private_and_never_overwritten(tmp_path: Path) -> None:
    destination = tmp_path / "logs" / "local-llm-spike.json"
    write_sanitized_report(not_run_report(), destination)
    mode = stat.S_IMODE(destination.stat().st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    with pytest.raises(FileExistsError):
        write_sanitized_report(not_run_report(), destination)
