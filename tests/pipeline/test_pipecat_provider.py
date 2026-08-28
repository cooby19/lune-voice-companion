from __future__ import annotations

import asyncio

import pytest
from pipecat.frames.frames import (
    Frame,
    FunctionCallInProgressFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    LLMThoughtTextFrame,
    MetricsFrame,
)
from pipecat.metrics.metrics import LLMTokenUsage, LLMUsageMetricsData
from pipecat.processors.frame_processor import FrameDirection

from lune.llm.contracts import (
    AttemptUsageFrame,
    GenerationFunctionCallFrame,
    GenerationLLMTextFrame,
    ProviderStreamFrame,
    ProviderTerminalFrame,
)
from lune.llm.prompt import ConversationMessage, PromptContext
from lune.llm.provider import DeterministicFakeLLMService
from lune.pipeline.pipecat_provider import PipecatAttemptProvider

CONTEXT = PromptContext(recent_messages=(ConversationMessage("user", "hello"),))


def usage_frame(
    *,
    prompt_tokens: int = 100,
    completion_tokens: int = 20,
    cache_read: int | None = None,
) -> MetricsFrame:
    return MetricsFrame(
        data=[
            LLMUsageMetricsData(
                processor="fake",
                model="gpt-5.6-terra",
                value=LLMTokenUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                    cache_read_input_tokens=cache_read,
                ),
            )
        ]
    )


class StallingFakeLLMService(DeterministicFakeLLMService):
    """Emit a prefix, then hold the response open the way a live stream would."""

    def __init__(self, frames: tuple[Frame, ...], *, late: Frame | None = None) -> None:
        super().__init__(frames)
        self.emitted = asyncio.Event()
        self.interruptions = 0
        self._late = late

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if isinstance(frame, InterruptionFrame):
            self.interruptions += 1
            await super().process_frame(frame, direction)
            if self._late is not None:
                await self.push_frame(self._late)
            return
        if not isinstance(frame, LLMContextFrame):
            await super().process_frame(frame, direction)
            return
        await super().process_frame(frame, direction)
        self.emitted.set()
        await asyncio.sleep(30)


async def collect(
    provider: PipecatAttemptProvider,
    *,
    generation_id: int = 1,
    attempt_id: str = "attempt-1",
) -> list[ProviderStreamFrame]:
    return [
        frame
        async for frame in provider.stream(
            generation_id=generation_id,
            attempt_id=attempt_id,
            context=CONTEXT,
        )
    ]


@pytest.mark.asyncio
async def test_pipeline_frames_become_typed_attempt_frames() -> None:
    provider = PipecatAttemptProvider(
        model="gpt-5.6-terra",
        service=DeterministicFakeLLMService(
            (
                LLMFullResponseStartFrame(),
                LLMTextFrame(text="一。"),
                LLMThoughtTextFrame(text="reasoning that must never be spoken"),
                LLMTextFrame(text="二\uff01"),
                usage_frame(cache_read=40),
                LLMFullResponseEndFrame(),
            )
        ),
    )
    try:
        frames = await collect(provider)
    finally:
        await provider.close()

    texts = [frame.text for frame in frames if isinstance(frame, GenerationLLMTextFrame)]
    assert texts == ["一。", "二\uff01"]
    assert all("reasoning" not in text for text in texts)

    usage = [frame for frame in frames if isinstance(frame, AttemptUsageFrame)]
    assert len(usage) == 1
    assert (usage[0].input_tokens, usage[0].cached_input_tokens) == (100, 40)
    assert usage[0].output_tokens == 20

    terminal = frames[-1]
    assert isinstance(terminal, ProviderTerminalFrame)
    assert terminal.status == "completed"
    assert terminal.generation_id == 1
    assert terminal.attempt_id == "attempt-1"


@pytest.mark.asyncio
async def test_an_inconsistent_cache_report_is_clamped_instead_of_crashing() -> None:
    provider = PipecatAttemptProvider(
        model="gpt-5.6-terra",
        service=DeterministicFakeLLMService(
            (
                LLMFullResponseStartFrame(),
                usage_frame(prompt_tokens=10, cache_read=50),
                LLMFullResponseEndFrame(),
            )
        ),
    )
    try:
        frames = await collect(provider)
    finally:
        await provider.close()

    usage = next(frame for frame in frames if isinstance(frame, AttemptUsageFrame))
    assert usage.input_tokens == 10
    assert usage.cached_input_tokens == 10
    assert usage.cache_write_input_tokens == 0


@pytest.mark.asyncio
async def test_function_calls_keep_their_generation_and_attempt() -> None:
    provider = PipecatAttemptProvider(
        model="gpt-5.6-terra",
        service=DeterministicFakeLLMService(
            (
                LLMFullResponseStartFrame(),
                FunctionCallInProgressFrame(
                    function_name="propose_memory",
                    tool_call_id="call-1",
                    arguments={"content": "private"},
                ),
                LLMFullResponseEndFrame(),
            )
        ),
    )
    try:
        frames = await collect(provider, generation_id=6, attempt_id="attempt-6")
    finally:
        await provider.close()

    call = next(frame for frame in frames if isinstance(frame, GenerationFunctionCallFrame))
    assert (call.generation_id, call.attempt_id) == (6, "attempt-6")
    assert call.function_name == "propose_memory"
    assert "private" not in repr(call)


@pytest.mark.asyncio
async def test_an_error_frame_becomes_a_transient_terminal() -> None:
    service = DeterministicFakeLLMService((LLMFullResponseStartFrame(),))
    provider = PipecatAttemptProvider(model="gpt-5.6-terra", service=service, stall_timeout_s=2.0)
    await provider.start()

    async def push_error() -> None:
        await asyncio.sleep(0.05)
        await service.push_error(error_msg="upstream refused")

    task = asyncio.create_task(push_error())
    try:
        frames = await collect(provider)
    finally:
        await task
        await provider.close()

    terminal = frames[-1]
    assert isinstance(terminal, ProviderTerminalFrame)
    assert terminal.status == "failed"
    assert terminal.transient is True
    assert terminal.error_code == "provider_error"


@pytest.mark.asyncio
async def test_a_stalled_provider_ends_the_attempt_instead_of_hanging() -> None:
    provider = PipecatAttemptProvider(
        model="gpt-5.6-terra",
        service=DeterministicFakeLLMService(()),
        stall_timeout_s=0.05,
    )
    try:
        frames = await collect(provider)
    finally:
        await provider.close()

    assert len(frames) == 1
    terminal = frames[0]
    assert isinstance(terminal, ProviderTerminalFrame)
    assert terminal.status == "incomplete"
    assert terminal.error_code == "stream_incomplete"


@pytest.mark.asyncio
async def test_the_fence_hook_stops_an_in_flight_stream_with_a_cancelled_terminal() -> None:
    service = StallingFakeLLMService(
        (LLMFullResponseStartFrame(), LLMTextFrame(text="一。")),
    )
    provider = PipecatAttemptProvider(
        model="gpt-5.6-terra",
        service=service,
        stall_timeout_s=5.0,
        drain_timeout_s=0.2,
    )
    await provider.start()

    collected: list[ProviderStreamFrame] = []

    async def consume() -> None:
        async for frame in provider.stream(
            generation_id=2,
            attempt_id="attempt-2",
            context=CONTEXT,
        ):
            collected.append(frame)

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(service.emitted.wait(), timeout=2.0)
        await asyncio.sleep(0.05)
        await provider.interrupt_and_drain(2)
        await asyncio.wait_for(consumer, timeout=2.0)
    finally:
        consumer.cancel()
        await provider.close()

    assert service.interruptions == 1
    terminal = collected[-1]
    assert isinstance(terminal, ProviderTerminalFrame)
    assert terminal.status == "cancelled"
    assert terminal.error_code == "cancelled"


@pytest.mark.asyncio
async def test_a_late_usage_report_survives_the_fence_and_is_drained_once() -> None:
    service = StallingFakeLLMService(
        (LLMFullResponseStartFrame(),),
        late=usage_frame(prompt_tokens=64, completion_tokens=8),
    )
    provider = PipecatAttemptProvider(
        model="gpt-5.6-terra",
        service=service,
        stall_timeout_s=0.05,
        drain_timeout_s=0.2,
    )
    try:
        await collect(provider, generation_id=3, attempt_id="attempt-3")
        await provider.interrupt_and_drain(3)
        drained = await provider.cancel_and_drain(generation_id=3, attempt_id="attempt-3")
    finally:
        await provider.close()

    usage = [frame for frame in drained if isinstance(frame, AttemptUsageFrame)]
    assert len(usage) == 1
    assert usage[0].input_tokens == 64
    assert usage[0].output_tokens == 8
    # The stash is consumed, so the generator's own call never re-interrupts.
    assert service.interruptions == 1
