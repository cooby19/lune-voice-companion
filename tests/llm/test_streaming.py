from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from lune.llm.budget import BudgetLedger
from lune.llm.contracts import (
    AttemptUsageFrame,
    GenerationFunctionCallFrame,
    GenerationLLMTextFrame,
    ProviderErrorCode,
    ProviderStreamFrame,
    ProviderTerminalFrame,
    TerminalStatus,
)
from lune.llm.prompt import ConversationMessage, PromptContext
from lune.llm.streaming import ConversationGenerator, ScriptedAttemptProvider

NOW = datetime(2026, 8, 27, 2, tzinfo=UTC)
CONTEXT = PromptContext(recent_messages=(ConversationMessage("user", "hello"),))


def text(value: str) -> Callable[[int, str], ProviderStreamFrame]:
    return lambda generation, attempt: GenerationLLMTextFrame(
        text=value,
        generation_id=generation,
        attempt_id=attempt,
    )


def usage(
    *, input_tokens: int = 100, output_tokens: int = 20
) -> Callable[[int, str], ProviderStreamFrame]:
    return lambda generation, attempt: AttemptUsageFrame(
        generation_id=generation,
        attempt_id=attempt,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def terminal(
    status: TerminalStatus = "completed",
    *,
    transient: bool = False,
    error_code: ProviderErrorCode | None = None,
) -> Callable[[int, str], ProviderStreamFrame]:
    return lambda generation, attempt: ProviderTerminalFrame(
        generation_id=generation,
        attempt_id=attempt,
        status=status,
        transient=transient,
        error_code=error_code,
    )


def tool() -> Callable[[int, str], ProviderStreamFrame]:
    return lambda generation, attempt: GenerationFunctionCallFrame(
        function_name="propose_memory",
        tool_call_id="tool-late",
        arguments={"content": "must not escape"},
        generation_id=generation,
        attempt_id=attempt,
    )


@pytest.mark.asyncio
async def test_success_cancels_and_drains_after_three_sentences() -> None:
    terra = ScriptedAttemptProvider(
        "gpt-5.6-terra",
        scripts=((text("一。二\uff01三\uff1f四。"),),),
        drains=((text("late token"), tool(), usage()),),
    )
    luna = ScriptedAttemptProvider("gpt-5.6-luna", scripts=())
    emitted: list[ProviderStreamFrame] = []
    ledger = BudgetLedger()
    generator = ConversationGenerator(
        providers={"gpt-5.6-terra": terra, "gpt-5.6-luna": luna},
        ledger=ledger,
        current_generation=lambda: 7,
        emit=_sink(emitted),
    )

    result = await generator.generate(
        generation_id=7,
        context=CONTEXT,
        at=NOW,
        max_input_tokens=8_000,
    )

    assert result.status == "completed"
    assert result.sentences_emitted == 3
    assert _text_output(emitted) == "一。二\uff01三\uff1f"
    assert not any(isinstance(frame, GenerationFunctionCallFrame) for frame in emitted)
    assert terra.cancelled_attempts == terra.drained_attempts
    assert len(ledger.settled_attempts) == 1
    assert not ledger.settled_attempts[0].estimated


@pytest.mark.asyncio
async def test_first_transient_error_retries_luna_and_accounts_both_attempts() -> None:
    terra = ScriptedAttemptProvider(
        "gpt-5.6-terra",
        scripts=((usage(input_tokens=10), terminal("failed", transient=True, error_code="busy")),),
    )
    luna = ScriptedAttemptProvider(
        "gpt-5.6-luna",
        scripts=((usage(input_tokens=20), text("fallback works。"), terminal()),),
    )
    emitted: list[ProviderStreamFrame] = []
    ledger = BudgetLedger()
    generator = ConversationGenerator(
        providers={"gpt-5.6-terra": terra, "gpt-5.6-luna": luna},
        ledger=ledger,
        current_generation=lambda: 3,
        emit=_sink(emitted),
    )

    result = await generator.generate(
        generation_id=3,
        context=CONTEXT,
        at=NOW,
        max_input_tokens=8_000,
    )

    assert result.status == "completed"
    assert result.models_attempted == ("gpt-5.6-terra", "gpt-5.6-luna")
    assert _text_output(emitted) == "fallback works。"
    assert len(ledger.settled_attempts) == 2
    assert all(not attempt.estimated for attempt in ledger.settled_attempts)


@pytest.mark.asyncio
async def test_error_after_playback_does_not_retry_and_uses_local_prompt() -> None:
    terra = ScriptedAttemptProvider(
        "gpt-5.6-terra",
        scripts=(
            (
                text("已播放一句。 "),
                terminal("failed", transient=True, error_code="connection_lost"),
            ),
        ),
    )
    luna = ScriptedAttemptProvider("gpt-5.6-luna", scripts=())
    emitted: list[ProviderStreamFrame] = []
    ledger = BudgetLedger()
    generator = ConversationGenerator(
        providers={"gpt-5.6-terra": terra, "gpt-5.6-luna": luna},
        ledger=ledger,
        current_generation=lambda: 5,
        emit=_sink(emitted),
    )

    result = await generator.generate(
        generation_id=5,
        context=CONTEXT,
        at=NOW,
        max_input_tokens=8_000,
    )

    assert result.status == "error"
    assert result.models_attempted == ("gpt-5.6-terra",)
    assert _text_output(emitted) == "已播放一句。 抱歉\uff0c回覆暫時中斷了。"
    assert ledger.settled_attempts[0].estimated


@pytest.mark.asyncio
async def test_generation_change_cancels_drain_and_rejects_late_token_and_tool() -> None:
    terra = ScriptedAttemptProvider(
        "gpt-5.6-terra",
        scripts=((text("first。 "), text("late。"), tool()),),
        drains=((text("drained late"), tool()),),
    )
    luna = ScriptedAttemptProvider("gpt-5.6-luna", scripts=())
    emitted: list[ProviderStreamFrame] = []
    generation = 9

    async def emit(frame: ProviderStreamFrame) -> None:
        nonlocal generation
        emitted.append(frame)
        generation = 10

    ledger = BudgetLedger()
    generator = ConversationGenerator(
        providers={"gpt-5.6-terra": terra, "gpt-5.6-luna": luna},
        ledger=ledger,
        current_generation=lambda: generation,
        emit=emit,
    )

    result = await generator.generate(
        generation_id=9,
        context=CONTEXT,
        at=NOW,
        max_input_tokens=8_000,
    )

    assert result.status == "cancelled"
    assert _text_output(emitted) == "first。 "
    assert not any(isinstance(frame, GenerationFunctionCallFrame) for frame in emitted)
    assert len(terra.cancelled_attempts) == 1
    assert ledger.settled_attempts[0].estimated


@pytest.mark.asyncio
async def test_usage_can_arrive_before_text_and_missing_usage_is_estimated() -> None:
    terra = ScriptedAttemptProvider(
        "gpt-5.6-terra",
        scripts=(
            (usage(input_tokens=25), text("first。"), terminal()),
            (text("second。"), terminal()),
        ),
    )
    luna = ScriptedAttemptProvider("gpt-5.6-luna", scripts=())
    emitted: list[ProviderStreamFrame] = []
    ledger = BudgetLedger()
    generator = ConversationGenerator(
        providers={"gpt-5.6-terra": terra, "gpt-5.6-luna": luna},
        ledger=ledger,
        current_generation=lambda: 1,
        emit=_sink(emitted),
    )

    first = await generator.generate(
        generation_id=1,
        context=CONTEXT,
        at=NOW,
        max_input_tokens=8_000,
    )
    second = await generator.generate(
        generation_id=1,
        context=CONTEXT,
        at=NOW,
        max_input_tokens=8_000,
    )

    assert first.status == second.status == "completed"
    assert not ledger.settled_attempts[0].estimated
    assert ledger.settled_attempts[1].estimated


def _sink(
    target: list[ProviderStreamFrame],
) -> Callable[[ProviderStreamFrame], Awaitable[None]]:
    async def emit(frame: ProviderStreamFrame) -> None:
        target.append(frame)

    return emit


def _text_output(frames: list[ProviderStreamFrame]) -> str:
    return "".join(frame.text for frame in frames if isinstance(frame, GenerationLLMTextFrame))


@pytest.mark.asyncio
async def test_a_pinned_primary_never_selects_or_retries_a_cloud_tier() -> None:
    local = ScriptedAttemptProvider(
        "qwen3.5-4b-q4-local",
        scripts=((text("在。"), usage(), terminal("failed", transient=True)),),
    )
    emitted: list[ProviderStreamFrame] = []
    # A month already past the fallback threshold would push Terra to Luna; a
    # pinned primary must not consult that logic at all.
    ledger = BudgetLedger(confirmed_twd={"2026-08": Decimal("800")})
    generator = ConversationGenerator(
        providers={"qwen3.5-4b-q4-local": local},
        ledger=ledger,
        current_generation=lambda: 9,
        emit=_sink(emitted),
        primary_model="qwen3.5-4b-q4-local",
    )

    result = await generator.generate(
        generation_id=9,
        context=CONTEXT,
        at=NOW,
        max_input_tokens=8_000,
    )

    assert result.models_attempted == ("qwen3.5-4b-q4-local",)
    assert ledger.total_with_reservations(NOW) == Decimal("800")


@pytest.mark.asyncio
async def test_a_repeating_script_survives_more_attempts_than_it_was_given() -> None:
    provider = ScriptedAttemptProvider(
        "gpt-5.6-terra",
        scripts=((text("嗨。"), terminal()),),
        repeat_last=True,
    )

    for attempt in ("attempt-1", "attempt-2", "attempt-3"):
        frames = [
            frame
            async for frame in provider.stream(
                generation_id=0,
                attempt_id=attempt,
                context=CONTEXT,
            )
        ]
        assert [type(frame).__name__ for frame in frames] == [
            "GenerationLLMTextFrame",
            "ProviderTerminalFrame",
        ]
