from pathlib import Path

import pytest

from lune.config import PersonaKernel
from lune.llm.contracts import (
    AttemptUsageFrame,
    GenerationFunctionCallFrame,
    GenerationLLMTextFrame,
    ProviderTerminalFrame,
)
from lune.llm.prompt import ConversationMessage, PromptContext, build_persona_instruction


def test_sensitive_provider_payloads_are_absent_from_repr() -> None:
    text = GenerationLLMTextFrame(text="私人回覆", generation_id=3, attempt_id="attempt-3")
    tool = GenerationFunctionCallFrame(
        function_name="propose_memory",
        tool_call_id="tool-1",
        arguments={"content": "私人記憶"},
        generation_id=3,
        attempt_id="attempt-3",
    )

    assert "私人回覆" not in repr(text)
    assert "私人記憶" not in repr(tool)


def test_usage_requires_attempt_correlation_and_consistent_details() -> None:
    with pytest.raises(ValueError, match="attempt ID"):
        AttemptUsageFrame(generation_id=1, attempt_id="", input_tokens=1)
    with pytest.raises(ValueError, match="cannot exceed"):
        AttemptUsageFrame(
            generation_id=1,
            attempt_id="attempt",
            input_tokens=10,
            cached_input_tokens=8,
            cache_write_input_tokens=3,
        )
    with pytest.raises(ValueError, match="finite error code"):
        ProviderTerminalFrame(
            generation_id=1,
            attempt_id="attempt",
            status="failed",
            error_code="raw private provider message",  # type: ignore[arg-type]
        )


def test_prompt_uses_validated_persona_and_bounded_minimum_context() -> None:
    persona = PersonaKernel.load(Path("examples/kernel.example.yaml"))
    instruction = build_persona_instruction(persona)
    context = PromptContext(
        summary="先前摘要",
        recent_messages=(ConversationMessage("user", "最近一句"),),
        relevant_memories=("偏好一", "偏好二"),
    )

    assert "Lune" in instruction
    assert "friend" in instruction
    assert "Never claim to be human" in instruction
    assert "先前摘要" not in repr(context)
    assert "最近一句" not in repr(context)
    assert "偏好一" not in repr(context)
    messages = context.to_pipecat().messages
    assert len(messages) == 3
    assert messages[-1] == {"role": "user", "content": "最近一句"}


def test_prompt_rejects_more_than_five_memories_or_oversized_memory_context() -> None:
    recent = (ConversationMessage("user", "hello"),)
    with pytest.raises(ValueError, match="five"):
        PromptContext(recent_messages=recent, relevant_memories=("x",) * 6)
    with pytest.raises(ValueError, match="1,200"):
        PromptContext(recent_messages=recent, relevant_memories=("x" * 1_201,))
