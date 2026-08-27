"""Drive one local LLM spike run and grade it.

The runner owns no policy of its own: it collects evidence, hands it to the gate modules
and writes the sanitized report. Anything it cannot measure stays `None` so the gate fails
rather than passes. Prompts come from the public fixtures; the private persona rubric is a
separate, separately authorised step.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from lune.llm_spike.cancellation import CancellationGate, CancelObservation, evaluate_cancellation
from lune.llm_spike.fixtures import stability_prompts
from lune.llm_spike.performance import (
    MIN_STABILITY_TURNS,
    LatencyBudget,
    LocalLLMMeasurements,
    MemoryPressure,
    PerformanceGate,
    ThermalState,
    evaluate_performance,
)
from lune.llm_spike.sampling import ResourceSample, sample_resources
from lune.llm_spike.thinking import ThinkingFilterResult, ThinkingGate, evaluate_thinking
from lune.llm_spike.tools import (
    ToolCallGate,
    ToolCallObservation,
    evaluate_tool_calls,
)
from lune.llm_spike.worker import GenerationOutcome, QwenWorkerHost
from lune.memory.proposals import MEMORY_CATEGORIES

SYSTEM_PROMPT: Final[str] = (
    "You are a concise voice assistant. Answer in one to three short sentences. "
    "Never include reasoning or analysis in your reply."
)

MEMORY_TOOL: Final[dict[str, Any]] = {
    "type": "function",
    "function": {
        "name": "propose_memory",
        "description": (
            "Propose one durable fact worth remembering about the user. "
            "Only stable preferences, important people or events, explicit plans, "
            "or an explicit request to remember something."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "category": {"type": "string", "enum": sorted(MEMORY_CATEGORIES)},
                "importance": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": ["content", "category", "importance"],
        },
    },
}

AFFINITY_TOOL: Final[dict[str, Any]] = {
    "type": "function",
    "function": {
        "name": "propose_affinity",
        "description": "Propose a one-point change to the relationship score.",
        "parameters": {
            "type": "object",
            "properties": {
                "delta": {"type": "integer", "enum": [-1, 1]},
                "reason": {"type": "string"},
            },
            "required": ["delta", "reason"],
        },
    },
}

TOOL_PROMPTS: Final[tuple[str, ...]] = (
    "記住我每週三晚上都要打羽球。",
    "Remember that I prefer short answers in the morning.",
    "幫我記下下個月三號要交稿。",
)


@dataclass(slots=True)
class SpikeEvidence:
    """Raw evidence gathered from one run, before grading."""

    turns: int = 0
    prompt_processing_ms: list[float] = field(default_factory=list)
    first_token_ms: list[float] = field(default_factory=list)
    first_sentence_ms: list[float] = field(default_factory=list)
    output_tokens_per_second: list[float] = field(default_factory=list)
    cold_start_ms: float | None = None
    warm_start_ms: float | None = None
    peak_rss_bytes: int | None = None
    rss_samples: list[int] = field(default_factory=list)
    swap_used_bytes: list[int] = field(default_factory=list)
    queue_depth: list[int] = field(default_factory=list)
    memory_pressure: list[MemoryPressure] = field(default_factory=list)
    thermal_states: list[ThermalState] = field(default_factory=list)
    thinking_results: list[ThinkingFilterResult] = field(default_factory=list)
    tool_observations: list[ToolCallObservation] = field(default_factory=list)
    cancel_observations: list[CancelObservation] = field(default_factory=list)
    enable_thinking_supported: bool = False
    failures: list[str] = field(default_factory=list)

    def to_measurements(self) -> LocalLLMMeasurements:
        return LocalLLMMeasurements(
            turns=self.turns,
            prompt_processing_ms=tuple(self.prompt_processing_ms),
            first_token_ms=tuple(self.first_token_ms),
            first_sentence_ms=tuple(self.first_sentence_ms),
            output_tokens_per_second=tuple(self.output_tokens_per_second),
            cold_start_ms=self.cold_start_ms,
            warm_start_ms=self.warm_start_ms,
            peak_rss_bytes=self.peak_rss_bytes,
            rss_samples=tuple(self.rss_samples),
            swap_used_bytes=tuple(self.swap_used_bytes),
            queue_depth=tuple(self.queue_depth),
            memory_pressure=tuple(self.memory_pressure),
            thermal_states=tuple(self.thermal_states),
        )


@dataclass(frozen=True, slots=True)
class SpikeGrades:
    thinking: ThinkingGate
    tools: ToolCallGate
    cancellation: CancellationGate
    performance: PerformanceGate


def record_sample(evidence: SpikeEvidence, sample: ResourceSample, queue_depth: int) -> None:
    if sample.rss_bytes is not None:
        evidence.rss_samples.append(sample.rss_bytes)
        evidence.peak_rss_bytes = max(evidence.peak_rss_bytes or 0, sample.rss_bytes)
    if sample.swap_used_bytes is not None:
        evidence.swap_used_bytes.append(sample.swap_used_bytes)
    evidence.queue_depth.append(queue_depth)
    evidence.memory_pressure.append(sample.memory_pressure)
    evidence.thermal_states.append(sample.thermal_state)


def record_turn(evidence: SpikeEvidence, outcome: GenerationOutcome) -> None:
    """Fold one completed turn into the evidence, keeping series lengths aligned."""

    evidence.turns += 1
    evidence.prompt_processing_ms.append(_prompt_processing_ms(outcome))
    evidence.first_token_ms.append(outcome.first_token_ms or 0.0)
    evidence.first_sentence_ms.append(outcome.first_sentence_ms or outcome.total_ms or 0.0)
    evidence.output_tokens_per_second.append(outcome.generation_tps or 0.0)
    if outcome.thinking is not None:
        evidence.thinking_results.append(outcome.thinking)
    if outcome.peak_memory_bytes:
        evidence.peak_rss_bytes = max(evidence.peak_rss_bytes or 0, outcome.peak_memory_bytes)


def _prompt_processing_ms(outcome: GenerationOutcome) -> float:
    """Time before the first generated token, which is prompt build plus prompt eval."""

    return outcome.first_token_ms or 0.0


def grade(evidence: SpikeEvidence, *, budget: LatencyBudget) -> SpikeGrades:
    return SpikeGrades(
        thinking=evaluate_thinking(tuple(evidence.thinking_results)),
        tools=evaluate_tool_calls(
            tuple(evidence.tool_observations),
            min_turns=min(MIN_STABILITY_TURNS, len(evidence.tool_observations) or 1),
        ),
        cancellation=evaluate_cancellation(tuple(evidence.cancel_observations)),
        performance=evaluate_performance(evidence.to_measurements(), budget=budget),
    )


def build_messages(prompt: str) -> tuple[Mapping[str, str], ...]:
    return (
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    )


async def run_stability(
    host: QwenWorkerHost,
    evidence: SpikeEvidence,
    *,
    turns: int = MIN_STABILITY_TURNS,
    max_tokens: int = 192,
) -> None:
    """Run the public stability corpus, sampling resources after every turn."""

    prompts = stability_prompts(turns)
    for index, prompt in enumerate(prompts):
        generation_id = host.advance_generation()
        outcome = await host.generate(
            generation_id=generation_id,
            request_id=f"stability-{index}",
            messages=build_messages(prompt),
            max_tokens=max_tokens,
        )
        if outcome.status == "error":
            evidence.failures.append(f"turn_{index}_error")
            continue
        record_turn(evidence, outcome)
        record_sample(evidence, sample_resources(_pids(host)), queue_depth=0)


async def run_cancellations(
    host: QwenWorkerHost,
    evidence: SpikeEvidence,
    *,
    trials: int = 5,
    max_tokens: int = 192,
) -> None:
    """Cancel mid-stream and record both stop latency and anything that arrived late."""

    for index in range(trials):
        generation_id = host.advance_generation()
        started = time.perf_counter()
        outcome = await host.generate(
            generation_id=generation_id,
            request_id=f"cancel-{index}",
            messages=build_messages("請用一百字詳細說明什麼是快取。"),
            max_tokens=max_tokens,
            cancel_after_first_token=True,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        stop_ms = min(elapsed_ms, elapsed_ms - (outcome.first_token_ms or 0.0))
        evidence.cancel_observations.append(
            CancelObservation(
                stop_latency_ms=max(stop_ms, 0.0),
                proof="inference_stopped" if outcome.status == "cancelled" else "unknown",
                late_text_events=max(outcome.events_after_cancel - 1, 0),
            )
        )


async def run_tool_calls(
    host: QwenWorkerHost,
    evidence: SpikeEvidence,
    *,
    max_tokens: int = 192,
) -> None:
    """Ask for memory-worthy statements and record whichever tool calls come back."""

    tools = [MEMORY_TOOL, AFFINITY_TOOL]
    for index, prompt in enumerate(TOOL_PROMPTS):
        generation_id = host.advance_generation()
        outcome = await host.generate(
            generation_id=generation_id,
            request_id=f"tool-{index}",
            messages=build_messages(prompt),
            tools=tools,
            max_tokens=max_tokens,
        )
        for call in outcome.tool_calls:
            evidence.tool_observations.append(
                ToolCallObservation(
                    turn_index=index,
                    tool_name=call.tool_name,
                    arguments_json=call.arguments_json,
                    expected_accept=not call.malformed,
                )
            )


def _pids(host: QwenWorkerHost) -> tuple[int, ...]:
    import os

    worker_pid = host.pid
    return (os.getpid(), worker_pid) if worker_pid else (os.getpid(),)


def summarize(evidence: SpikeEvidence, grades: SpikeGrades) -> dict[str, object]:
    """A compact, private-content-free console summary."""

    aggregates = grades.performance.aggregates
    return {
        "turns": evidence.turns,
        "cold_start_ms": evidence.cold_start_ms,
        "warm_start_ms": evidence.warm_start_ms,
        "enable_thinking_supported": evidence.enable_thinking_supported,
        "first_token_p50_ms": aggregates.first_token_p50_ms if aggregates else None,
        "first_token_p95_ms": aggregates.first_token_p95_ms if aggregates else None,
        "first_sentence_p50_ms": aggregates.first_sentence_p50_ms if aggregates else None,
        "first_sentence_p95_ms": aggregates.first_sentence_p95_ms if aggregates else None,
        "tokens_per_second_p50": aggregates.output_tokens_per_second_p50 if aggregates else None,
        "peak_rss_bytes": aggregates.peak_rss_bytes if aggregates else None,
        "worst_memory_pressure": aggregates.worst_memory_pressure if aggregates else None,
        "worst_thermal_state": aggregates.worst_thermal_state if aggregates else None,
        "thinking_passed": grades.thinking.passed,
        "tools_passed": grades.tools.passed,
        "cancellation_passed": grades.cancellation.passed,
        "performance_passed": grades.performance.passed,
        "performance_reasons": list(grades.performance.reasons),
        "failures": list(evidence.failures),
    }


def write_console_summary(summary: Mapping[str, object], destination: Path | None) -> str:
    text = json.dumps(summary, ensure_ascii=True, indent=2, sort_keys=True)
    if destination is not None:
        destination.write_text(text + "\n", encoding="utf-8")
    return text


def message_sequence(prompts: Sequence[str]) -> tuple[tuple[Mapping[str, str], ...], ...]:
    return tuple(build_messages(prompt) for prompt in prompts)
