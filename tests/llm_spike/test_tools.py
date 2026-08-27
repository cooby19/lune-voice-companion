from __future__ import annotations

import json

from lune.llm_spike.performance import MIN_STABILITY_TURNS
from lune.llm_spike.tools import (
    MAX_ARGUMENTS_BYTES,
    PROPOSE_AFFINITY,
    PROPOSE_MEMORY,
    ToolCallObservation,
    ToolCallValidator,
    evaluate_tool_calls,
    normalize_content,
)

PRIVATE = "使用者說他每週三晚上打羽球"


def memory_args(content: str = PRIVATE, **overrides: object) -> str:
    payload: dict[str, object] = {
        "content": content,
        "category": "stable_preference",
        "importance": 0.7,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def affinity_args(**overrides: object) -> str:
    payload: dict[str, object] = {"delta": 1, "reason": "warm exchange"}
    payload.update(overrides)
    return json.dumps(payload)


def fresh() -> ToolCallValidator:
    validator = ToolCallValidator()
    validator.begin_turn(0)
    return validator


def test_valid_memory_and_affinity_calls_are_accepted() -> None:
    validator = fresh()
    assert validator.validate(PROPOSE_MEMORY, memory_args()).accepted
    assert validator.validate(PROPOSE_AFFINITY, affinity_args()).accepted


def test_unknown_tool_is_refused() -> None:
    outcome = fresh().validate("write_memory", memory_args())
    assert outcome.reason == "unknown_tool"


def test_malformed_json_is_refused() -> None:
    assert fresh().validate(PROPOSE_MEMORY, "{not json").reason == "arguments_not_json"


def test_non_object_arguments_are_refused() -> None:
    assert fresh().validate(PROPOSE_MEMORY, "[1, 2]").reason == "arguments_not_object"


def test_oversized_arguments_are_refused() -> None:
    outcome = fresh().validate(PROPOSE_MEMORY, memory_args("x" * MAX_ARGUMENTS_BYTES))
    assert outcome.reason == "arguments_too_large"


def test_extra_and_missing_fields_are_refused() -> None:
    validator = fresh()
    extra = json.loads(memory_args())
    extra["confidence"] = 0.9
    assert validator.validate(PROPOSE_MEMORY, json.dumps(extra)).reason == "unexpected_field"
    incomplete = json.dumps({"content": PRIVATE, "category": "stable_preference"})
    assert validator.validate(PROPOSE_MEMORY, incomplete).reason == "missing_field"


def test_unsupported_category_is_refused() -> None:
    outcome = fresh().validate(PROPOSE_MEMORY, memory_args(category="mood_guess"))
    assert outcome.reason == "category_invalid"


def test_importance_must_be_a_number_between_zero_and_one() -> None:
    validator = fresh()
    assert validator.validate(PROPOSE_MEMORY, memory_args(importance=1.5)).reason == (
        "importance_invalid"
    )
    assert validator.validate(PROPOSE_MEMORY, memory_args(importance="high")).reason == (
        "importance_invalid"
    )
    assert validator.validate(PROPOSE_MEMORY, memory_args(importance=True)).reason == (
        "importance_invalid"
    )


def test_empty_content_is_refused() -> None:
    assert fresh().validate(PROPOSE_MEMORY, memory_args("   ")).reason == "content_invalid"


def test_affinity_delta_is_limited_to_plus_or_minus_one() -> None:
    validator = fresh()
    assert validator.validate(PROPOSE_AFFINITY, affinity_args(delta=3)).reason == "delta_invalid"
    assert validator.validate(PROPOSE_AFFINITY, affinity_args(delta=0)).reason == "delta_invalid"
    assert validator.validate(PROPOSE_AFFINITY, affinity_args(delta=True)).reason == (
        "delta_invalid"
    )


def test_affinity_requires_a_reason() -> None:
    assert fresh().validate(PROPOSE_AFFINITY, affinity_args(reason="")).reason == "reason_invalid"


def test_second_call_in_the_same_turn_is_refused() -> None:
    validator = fresh()
    assert validator.validate(PROPOSE_MEMORY, memory_args()).accepted
    second = validator.validate(PROPOSE_MEMORY, memory_args("另一件事"))
    assert second.reason == "per_turn_limit_exceeded"
    assert validator.validate(PROPOSE_AFFINITY, affinity_args()).accepted
    assert validator.validate(PROPOSE_AFFINITY, affinity_args()).reason == (
        "per_turn_limit_exceeded"
    )


def test_duplicate_content_across_turns_is_refused() -> None:
    validator = ToolCallValidator()
    validator.begin_turn(0)
    assert validator.validate(PROPOSE_MEMORY, memory_args()).accepted
    validator.begin_turn(1)
    repeated = validator.validate(PROPOSE_MEMORY, memory_args(f"  {PRIVATE}  "))
    assert repeated.reason == "duplicate_content"


def test_normalize_content_folds_spacing_and_case() -> None:
    assert normalize_content("  Hello   World  ") == "hello world"


def test_observation_repr_hides_arguments() -> None:
    observation = ToolCallObservation(
        turn_index=0, tool_name=PROPOSE_MEMORY, arguments_json=PRIVATE
    )
    assert PRIVATE not in repr(observation)


def full_run() -> tuple[ToolCallObservation, ...]:
    return tuple(
        ToolCallObservation(
            turn_index=index,
            tool_name=PROPOSE_MEMORY,
            arguments_json=memory_args(f"事件 {index}"),
            expected_accept=True,
        )
        for index in range(MIN_STABILITY_TURNS)
    )


def test_gate_passes_on_a_stable_multi_turn_run() -> None:
    gate = evaluate_tool_calls(full_run())
    assert gate.passed, gate.reasons
    assert gate.turns_observed == MIN_STABILITY_TURNS
    assert gate.accepted_calls == MIN_STABILITY_TURNS
    assert gate.malformed_ratio == 0.0


def test_gate_fails_when_the_host_accepts_a_call_it_should_reject() -> None:
    observations = (
        *full_run(),
        ToolCallObservation(
            turn_index=MIN_STABILITY_TURNS,
            tool_name=PROPOSE_MEMORY,
            arguments_json=memory_args("新的一件事"),
            expected_accept=False,
        ),
    )
    gate = evaluate_tool_calls(observations)
    assert not gate.passed
    assert "invalid_call_accepted" in gate.reasons


def test_gate_fails_when_a_valid_call_is_rejected() -> None:
    observations = (
        *full_run(),
        ToolCallObservation(
            turn_index=MIN_STABILITY_TURNS,
            tool_name="delete_memory",
            arguments_json="{}",
            expected_accept=True,
        ),
    )
    gate = evaluate_tool_calls(observations)
    assert not gate.passed
    assert "valid_call_rejected" in gate.reasons


def test_gate_fails_a_short_run() -> None:
    gate = evaluate_tool_calls(full_run()[:5])
    assert "turns_insufficient" in gate.reasons


def test_gate_fails_when_no_call_is_usable() -> None:
    observations = tuple(
        ToolCallObservation(
            turn_index=index,
            tool_name="unsupported",
            arguments_json="{}",
            expected_accept=False,
        )
        for index in range(MIN_STABILITY_TURNS)
    )
    gate = evaluate_tool_calls(observations)
    assert not gate.passed
    assert "no_usable_tool_calls" in gate.reasons
    assert gate.malformed_ratio == 1.0


def test_gate_without_observations_is_not_a_pass() -> None:
    gate = evaluate_tool_calls(())
    assert not gate.evaluated
    assert not gate.passed
    assert gate.reasons == ("no_observations",)
