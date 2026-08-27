"""Explicitly allowlisted, privacy-safe local LLM spike reports."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lune.llm_spike.cancellation import CancellationGate
from lune.llm_spike.decision import DecisionReason, LocalProviderDecision, SpikeProviderChoice
from lune.llm_spike.model_pin import LocalLLMManifestReason
from lune.llm_spike.performance import GrowthCheck, LocalLLMAggregates, PerformanceGate
from lune.llm_spike.runtime import LocalRuntimeName
from lune.llm_spike.thinking import ThinkingGate
from lune.llm_spike.tools import ToolCallGate

type GateStatus = Literal["not_run", "passed", "failed"]


@dataclass(frozen=True, slots=True)
class SanitizedLocalLLMReport:
    manifest_status: LocalLLMManifestReason
    thinking_status: GateStatus
    tool_call_status: GateStatus
    cancellation_status: GateStatus
    performance_status: GateStatus
    provider: SpikeProviderChoice
    local_enabled: bool
    selected_runtime: LocalRuntimeName | None
    declared_remote_cancel: bool
    decision_reasons: tuple[DecisionReason, ...]
    gate_reasons: tuple[str, ...]
    aggregates: LocalLLMAggregates | None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "manifest_status": self.manifest_status,
            "thinking_status": self.thinking_status,
            "tool_call_status": self.tool_call_status,
            "cancellation_status": self.cancellation_status,
            "performance_status": self.performance_status,
            "provider": self.provider,
            "local_enabled": self.local_enabled,
            "selected_runtime": self.selected_runtime,
            "declared_remote_cancel": self.declared_remote_cancel,
            "decision_reasons": list(self.decision_reasons),
            "gate_reasons": list(self.gate_reasons),
            "metrics": _metrics(self.aggregates),
        }


def _growth(check: GrowthCheck) -> dict[str, object]:
    return {
        "samples": check.samples,
        "strictly_increasing": check.strictly_increasing,
        "growth_ratio": check.growth_ratio,
        "accumulating": check.accumulating,
    }


def _metrics(aggregates: LocalLLMAggregates | None) -> dict[str, object] | None:
    if aggregates is None:
        return None
    return {
        "turns": aggregates.turns,
        "cold_start_ms": aggregates.cold_start_ms,
        "warm_start_ms": aggregates.warm_start_ms,
        "prompt_processing_p95_ms": aggregates.prompt_processing_p95_ms,
        "first_token_p50_ms": aggregates.first_token_p50_ms,
        "first_token_p95_ms": aggregates.first_token_p95_ms,
        "first_sentence_p50_ms": aggregates.first_sentence_p50_ms,
        "first_sentence_p95_ms": aggregates.first_sentence_p95_ms,
        "first_sentence_budget_ms": aggregates.first_sentence_budget_ms,
        "output_tokens_per_second_p50": aggregates.output_tokens_per_second_p50,
        "peak_rss_bytes": aggregates.peak_rss_bytes,
        "peak_swap_bytes": aggregates.peak_swap_bytes,
        "worst_memory_pressure": aggregates.worst_memory_pressure,
        "worst_thermal_state": aggregates.worst_thermal_state,
        "rss_growth": _growth(aggregates.rss_growth),
        "swap_growth": _growth(aggregates.swap_growth),
        "queue_growth": _growth(aggregates.queue_growth),
    }


def _status(evaluated: bool, passed: bool) -> GateStatus:
    if not evaluated:
        return "not_run"
    return "passed" if passed else "failed"


def build_sanitized_report(
    *,
    manifest_status: LocalLLMManifestReason,
    thinking_gate: ThinkingGate,
    tool_gate: ToolCallGate,
    cancellation_gate: CancellationGate,
    performance_gate: PerformanceGate,
    decision: LocalProviderDecision,
) -> SanitizedLocalLLMReport:
    gate_reasons: list[str] = []
    gate_reasons.extend(thinking_gate.reasons)
    gate_reasons.extend(tool_gate.reasons)
    gate_reasons.extend(cancellation_gate.reasons)
    gate_reasons.extend(performance_gate.reasons)
    return SanitizedLocalLLMReport(
        manifest_status=manifest_status,
        thinking_status=_status(thinking_gate.evaluated, thinking_gate.passed),
        tool_call_status=_status(tool_gate.evaluated, tool_gate.passed),
        cancellation_status=_status(cancellation_gate.evaluated, cancellation_gate.passed),
        performance_status=_status(performance_gate.evaluated, performance_gate.passed),
        provider=decision.provider,
        local_enabled=decision.local_enabled,
        selected_runtime=decision.selected_runtime,
        declared_remote_cancel=decision.declared_remote_cancel,
        decision_reasons=decision.reasons,
        gate_reasons=tuple(gate_reasons),
        aggregates=performance_gate.aggregates,
    )


def write_sanitized_report(report: SanitizedLocalLLMReport, destination: Path) -> None:
    """Create, never overwrite, a mode-0600 report containing allowlisted fields only."""

    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.parent.chmod(0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    file_descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", closefd=False) as handle:
            json.dump(report.to_dict(), handle, ensure_ascii=True, separators=(",", ":"))
            handle.write("\n")
    finally:
        os.close(file_descriptor)
