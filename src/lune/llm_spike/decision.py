"""Decide whether a local provider may enter M6, defaulting to the shipped OpenAI path.

Registering a local provider name in `lune.llm.contracts.ProviderName` is deliberately not
done here. Until every gate passes, the released pipeline keeps using
`OpenAIResponsesLLMService`, and adopting a runtime that adds a fourth managed process is a
product decision rather than something this module may settle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from lune.llm_spike.cancellation import CancellationGate
from lune.llm_spike.model_pin import LocalLLMManifestCheck
from lune.llm_spike.performance import PerformanceGate
from lune.llm_spike.runtime import LocalRuntimeName, RuntimeProbe
from lune.llm_spike.thinking import ThinkingGate
from lune.llm_spike.tools import ToolCallGate

type SpikeProviderChoice = Literal["openai_responses", "local_qwen"]
type DecisionReason = Literal[
    "runtime_not_selected",
    "model_pin_not_established",
    "model_manifest_unavailable",
    "thinking_not_run",
    "thinking_gate_failed",
    "tool_calls_not_run",
    "tool_call_gate_failed",
    "cancellation_not_run",
    "cancellation_gate_failed",
    "performance_not_run",
    "performance_gate_failed",
    "local_qwen_eligible",
]


@dataclass(frozen=True, slots=True)
class LocalProviderDecision:
    provider: SpikeProviderChoice
    local_enabled: bool
    selected_runtime: LocalRuntimeName | None
    declared_remote_cancel: bool
    reasons: tuple[DecisionReason, ...]


def decide_local_provider(
    *,
    runtime_probes: tuple[RuntimeProbe, ...],
    manifest_check: LocalLLMManifestCheck,
    thinking_gate: ThinkingGate,
    tool_gate: ToolCallGate,
    cancellation_gate: CancellationGate,
    performance_gate: PerformanceGate,
) -> LocalProviderDecision:
    """Return `local_qwen` only when a runtime is usable and every gate has passed."""

    usable = tuple(probe for probe in runtime_probes if probe.usable)
    selected = usable[0].name if len(usable) == 1 else None

    reasons: list[DecisionReason] = []
    if selected is None:
        reasons.append("runtime_not_selected")
    if not manifest_check.pin_established:
        reasons.append("model_pin_not_established")
    elif not manifest_check.ready:
        reasons.append("model_manifest_unavailable")
    _grade(reasons, thinking_gate.evaluated, thinking_gate.passed, "thinking")
    _grade(reasons, tool_gate.evaluated, tool_gate.passed, "tool_call")
    _grade(reasons, cancellation_gate.evaluated, cancellation_gate.passed, "cancellation")
    _grade(reasons, performance_gate.evaluated, performance_gate.passed, "performance")

    if reasons:
        return LocalProviderDecision(
            provider="openai_responses",
            local_enabled=False,
            selected_runtime=None,
            declared_remote_cancel=False,
            reasons=tuple(reasons),
        )
    return LocalProviderDecision(
        provider="local_qwen",
        local_enabled=True,
        selected_runtime=selected,
        declared_remote_cancel=cancellation_gate.declared_remote_cancel,
        reasons=("local_qwen_eligible",),
    )


def _grade(reasons: list[DecisionReason], evaluated: bool, passed: bool, prefix: str) -> None:
    not_run: dict[str, DecisionReason] = {
        "thinking": "thinking_not_run",
        "tool_call": "tool_calls_not_run",
        "cancellation": "cancellation_not_run",
        "performance": "performance_not_run",
    }
    failed: dict[str, DecisionReason] = {
        "thinking": "thinking_gate_failed",
        "tool_call": "tool_call_gate_failed",
        "cancellation": "cancellation_gate_failed",
        "performance": "performance_gate_failed",
    }
    if not evaluated:
        reasons.append(not_run[prefix])
    elif not passed:
        reasons.append(failed[prefix])
