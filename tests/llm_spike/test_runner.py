from __future__ import annotations

from lune.llm_spike.performance import LatencyBudget
from lune.llm_spike.runner import (
    AFFINITY_TOOL,
    MEMORY_TOOL,
    SpikeEvidence,
    build_messages,
    grade,
    record_sample,
    record_turn,
    summarize,
)
from lune.llm_spike.sampling import ResourceSample
from lune.llm_spike.thinking import ThinkingFilterResult
from lune.llm_spike.worker import GenerationOutcome

PRIVATE = "使用者的私人回覆內容"


def outcome(**overrides: object) -> GenerationOutcome:
    base: dict[str, object] = {
        "generation_id": 1,
        "status": "completed",
        "text": PRIVATE,
        "thinking": ThinkingFilterResult(text=PRIVATE, violations=()),
        "first_token_ms": 120.0,
        "first_sentence_ms": 400.0,
        "total_ms": 900.0,
        "generation_tps": 45.0,
        "peak_memory_bytes": 5 * 1024**3,
    }
    base.update(overrides)
    return GenerationOutcome(**base)  # type: ignore[arg-type]


def test_recording_turns_keeps_every_series_aligned() -> None:
    evidence = SpikeEvidence()
    for _ in range(4):
        record_turn(evidence, outcome())
    measurements = evidence.to_measurements()
    assert measurements.turns == 4
    assert len(measurements.first_token_ms) == 4
    assert len(measurements.first_sentence_ms) == 4
    assert len(measurements.output_tokens_per_second) == 4
    assert len(measurements.prompt_processing_ms) == 4


def test_missing_timings_become_zero_not_a_crash() -> None:
    evidence = SpikeEvidence()
    record_turn(evidence, outcome(first_token_ms=None, first_sentence_ms=None, generation_tps=None))
    measurements = evidence.to_measurements()
    assert measurements.first_token_ms == (0.0,)
    assert measurements.first_sentence_ms == (900.0,)


def test_peak_memory_tracks_the_maximum() -> None:
    evidence = SpikeEvidence()
    record_turn(evidence, outcome(peak_memory_bytes=3 * 1024**3))
    record_turn(evidence, outcome(peak_memory_bytes=6 * 1024**3))
    record_turn(evidence, outcome(peak_memory_bytes=4 * 1024**3))
    assert evidence.peak_rss_bytes == 6 * 1024**3


def test_resource_samples_are_recorded_and_bounded() -> None:
    evidence = SpikeEvidence()
    record_sample(
        evidence,
        ResourceSample(
            rss_bytes=1024,
            swap_used_bytes=2048,
            memory_pressure="normal",
            thermal_state="fair",
        ),
        queue_depth=2,
    )
    assert evidence.rss_samples == [1024]
    assert evidence.swap_used_bytes == [2048]
    assert evidence.queue_depth == [2]
    assert evidence.memory_pressure == ["normal"]
    assert evidence.thermal_states == ["fair"]


def test_missing_resource_readings_are_simply_absent() -> None:
    evidence = SpikeEvidence()
    record_sample(
        evidence,
        ResourceSample(
            rss_bytes=None,
            swap_used_bytes=None,
            memory_pressure="unknown",
            thermal_state="unknown",
        ),
        queue_depth=0,
    )
    assert evidence.rss_samples == []
    assert evidence.swap_used_bytes == []
    assert evidence.memory_pressure == ["unknown"]


def test_an_empty_run_fails_every_gate() -> None:
    grades = grade(SpikeEvidence(), budget=LatencyBudget())
    assert not grades.thinking.passed
    assert not grades.tools.passed
    assert not grades.cancellation.passed
    assert not grades.performance.passed


def test_summary_contains_no_private_text() -> None:
    evidence = SpikeEvidence()
    record_turn(evidence, outcome())
    grades = grade(evidence, budget=LatencyBudget())
    summary = summarize(evidence, grades)
    assert PRIVATE not in repr(summary)
    assert summary["turns"] == 1
    assert summary["performance_passed"] is False


def test_messages_carry_a_system_prompt_and_the_user_turn() -> None:
    messages = build_messages("你好")
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "你好"}


def test_tool_definitions_match_the_host_contract() -> None:
    assert MEMORY_TOOL["function"]["name"] == "propose_memory"  # type: ignore[index]
    assert AFFINITY_TOOL["function"]["name"] == "propose_affinity"  # type: ignore[index]
    delta = AFFINITY_TOOL["function"]["parameters"]["properties"]["delta"]  # type: ignore[index]
    assert delta["enum"] == [-1, 1]
