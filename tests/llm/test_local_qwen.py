from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import pytest

from lune.llm.contracts import (
    AttemptUsageFrame,
    GenerationFunctionCallFrame,
    GenerationLLMTextFrame,
    ProviderStreamFrame,
    ProviderTerminalFrame,
)
from lune.llm.local_qwen import LocalQwenLLMService
from lune.llm.prompt import ConversationMessage, PromptContext
from lune.llm_spike.tools import ExtractedToolCall
from lune.llm_spike.worker import GenerationOutcome, WorkerError
from lune.pipeline.pipecat_provider import PipecatAttemptProvider

CONTEXT = PromptContext(
    recent_messages=(ConversationMessage("user", "你好"),),
    summary="local summary",
    relevant_memories=("記得帶環保袋",),
)


class FakeWorkerHost:
    """Stand in for the isolated worker without spawning a process or loading weights."""

    def __init__(
        self,
        *,
        chunks: Sequence[str] = (),
        tool_calls: Sequence[ExtractedToolCall] = (),
        prompt_tokens: int | None = 120,
        generation_tokens: int | None = 18,
        error: WorkerError | None = None,
        status: str = "completed",
        hold_until_cancelled: bool = False,
    ) -> None:
        self._chunks = tuple(chunks)
        self._tool_calls = tuple(tool_calls)
        self._prompt_tokens = prompt_tokens
        self._generation_tokens = generation_tokens
        self._error = error
        self._status = status
        self._hold = hold_until_cancelled
        self._generation = 0
        self._cancel_arrived = asyncio.Event()
        self.streaming = asyncio.Event()
        self.starts = 0
        self.closes = 0
        self.cancelled: list[int] = []
        self.messages: tuple[Mapping[str, str], ...] = ()
        self.tools: tuple[Mapping[str, Any], ...] = ()
        self.max_tokens: int | None = None

    async def start(self) -> None:
        self.starts += 1

    def advance_generation(self) -> int:
        self._generation += 1
        return self._generation

    async def generate(
        self,
        *,
        generation_id: int,
        request_id: str,
        messages: Sequence[Mapping[str, str]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        max_tokens: int = 192,
        cancel_after_first_token: bool = False,
        on_text: Callable[[str], Awaitable[None]] | None = None,
    ) -> GenerationOutcome:
        del request_id, cancel_after_first_token
        self.messages = tuple(messages)
        self.tools = tuple(tools or ())
        self.max_tokens = max_tokens
        if self._error is not None:
            raise self._error
        for chunk in self._chunks:
            if on_text is not None:
                await on_text(chunk)
        self.streaming.set()
        if self._hold:
            await self._cancel_arrived.wait()
            return GenerationOutcome(generation_id=generation_id, status="cancelled")
        return GenerationOutcome(
            generation_id=generation_id,
            status=self._status,  # type: ignore[arg-type]
            text="".join(self._chunks),
            tool_calls=self._tool_calls,
            prompt_tokens=self._prompt_tokens,
            generation_tokens=self._generation_tokens,
        )

    async def cancel(self, generation_id: int) -> None:
        self.cancelled.append(generation_id)
        self._cancel_arrived.set()

    async def close(self) -> None:
        self.closes += 1


def build_service(host: FakeWorkerHost, **kwargs: Any) -> LocalQwenLLMService:
    return LocalQwenLLMService(
        host=host,  # type: ignore[arg-type]
        system_instruction=kwargs.pop("system_instruction", "private persona"),
        **kwargs,
    )


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
async def test_worker_output_becomes_the_same_typed_attempt_stream_as_the_cloud() -> None:
    host = FakeWorkerHost(
        chunks=("你好\uff0c", "我在。"),
        tool_calls=(
            ExtractedToolCall(
                tool_name="propose_memory",
                arguments_json='{"content":"private","category":"preference","importance":0.5}',
            ),
        ),
    )
    provider = PipecatAttemptProvider(model="qwen3.5-4b-q4-local", service=build_service(host))
    try:
        frames = await collect(provider)
    finally:
        await provider.close()

    texts = [frame.text for frame in frames if isinstance(frame, GenerationLLMTextFrame)]
    assert texts == ["你好\uff0c", "我在。"]

    call = next(frame for frame in frames if isinstance(frame, GenerationFunctionCallFrame))
    assert call.function_name == "propose_memory"
    assert "private" not in repr(call)

    usage = next(frame for frame in frames if isinstance(frame, AttemptUsageFrame))
    assert (usage.input_tokens, usage.output_tokens) == (120, 18)
    assert usage.cached_input_tokens == 0

    terminal = frames[-1]
    assert isinstance(terminal, ProviderTerminalFrame)
    assert terminal.status == "completed"


@pytest.mark.asyncio
async def test_a_malformed_tool_block_never_reaches_the_pipeline() -> None:
    host = FakeWorkerHost(
        chunks=("好。",),
        tool_calls=(
            ExtractedToolCall(tool_name="propose_memory", arguments_json="{", malformed=True),
        ),
    )
    provider = PipecatAttemptProvider(model="qwen3.5-4b-q4-local", service=build_service(host))
    try:
        frames = await collect(provider)
    finally:
        await provider.close()

    assert not [frame for frame in frames if isinstance(frame, GenerationFunctionCallFrame)]


@pytest.mark.asyncio
async def test_the_persona_leads_and_local_context_arrives_as_system_text() -> None:
    host = FakeWorkerHost(chunks=("好。",))
    provider = PipecatAttemptProvider(model="qwen3.5-4b-q4-local", service=build_service(host))
    try:
        await collect(provider)
    finally:
        await provider.close()

    roles = [message["role"] for message in host.messages]
    assert roles[0] == "system"
    assert host.messages[0]["content"] == "private persona"
    # The bounded local context uses Pipecat's developer role, which the chat
    # template has no slot for.
    assert "developer" not in roles
    assert roles[-1] == "user"


@pytest.mark.asyncio
async def test_both_proposal_tools_are_offered_and_the_output_bound_is_passed_through() -> None:
    host = FakeWorkerHost(chunks=("好。",))
    provider = PipecatAttemptProvider(
        model="qwen3.5-4b-q4-local",
        service=build_service(host, max_output_tokens=64),
    )
    try:
        await collect(provider)
    finally:
        await provider.close()

    offered = [tool["function"]["name"] for tool in host.tools]
    assert offered == ["propose_memory", "propose_affinity"]
    assert host.max_tokens == 64


@pytest.mark.asyncio
async def test_a_worker_fault_becomes_a_transient_terminal_the_turn_can_retry() -> None:
    host = FakeWorkerHost(error=WorkerError("worker_eof"))
    provider = PipecatAttemptProvider(
        model="qwen3.5-4b-q4-local",
        service=build_service(host),
        stall_timeout_s=2.0,
    )
    try:
        frames = await collect(provider)
    finally:
        await provider.close()

    terminal = frames[-1]
    assert isinstance(terminal, ProviderTerminalFrame)
    assert terminal.status == "failed"
    assert terminal.transient is True
    assert terminal.error_code == "provider_error"


@pytest.mark.asyncio
async def test_the_fence_cancels_the_worker_and_no_completed_terminal_follows() -> None:
    host = FakeWorkerHost(chunks=("一。",), hold_until_cancelled=True)
    provider = PipecatAttemptProvider(
        model="qwen3.5-4b-q4-local",
        service=build_service(host),
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
        await asyncio.wait_for(host.streaming.wait(), timeout=2.0)
        await provider.interrupt_and_drain(2)
        await asyncio.wait_for(consumer, timeout=2.0)
    finally:
        consumer.cancel()
        await provider.close()

    assert host.cancelled == [1]
    terminal = collected[-1]
    assert isinstance(terminal, ProviderTerminalFrame)
    assert terminal.status == "cancelled"
    assert not [frame for frame in collected if isinstance(frame, AttemptUsageFrame)]


@pytest.mark.asyncio
async def test_preloading_is_idempotent_and_closing_releases_the_weights() -> None:
    host = FakeWorkerHost()
    service = build_service(host)

    await service.preload()
    await service.preload()
    assert host.starts == 1

    await service.close()
    assert host.closes == 1

    await service.preload()
    assert host.starts == 2


def test_the_service_refuses_an_empty_persona_or_an_out_of_range_bound() -> None:
    host = FakeWorkerHost()
    with pytest.raises(ValueError):
        build_service(host, system_instruction="")
    with pytest.raises(ValueError):
        build_service(host, max_output_tokens=193)


class TwoPhaseWorkerHost:
    """Hold the first generation open until cancelled, then answer the next at once."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled: list[int] = []
        self.generations: list[int] = []
        self._generation = 0
        self._holding: asyncio.Event | None = None

    async def start(self) -> None:
        return None

    def advance_generation(self) -> int:
        self._generation += 1
        return self._generation

    async def generate(
        self,
        *,
        generation_id: int,
        request_id: str,
        messages: Sequence[Mapping[str, str]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        max_tokens: int = 192,
        cancel_after_first_token: bool = False,
        on_text: Callable[[str], Awaitable[None]] | None = None,
    ) -> GenerationOutcome:
        del request_id, messages, tools, max_tokens, cancel_after_first_token
        self.generations.append(generation_id)
        if len(self.generations) == 1:
            self._holding = asyncio.Event()
            self.started.set()
            await self._holding.wait()
            return GenerationOutcome(generation_id=generation_id, status="cancelled")
        if on_text is not None:
            await on_text("好。")
        return GenerationOutcome(generation_id=generation_id, status="completed", text="好。")

    async def cancel(self, generation_id: int) -> None:
        self.cancelled.append(generation_id)
        if self._holding is not None:
            self._holding.set()

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_an_out_of_band_completion_pushes_no_frames_and_offers_no_tools() -> None:
    host = FakeWorkerHost(chunks=("週末的旅行計畫",))
    service = build_service(host)
    pushed: list[object] = []

    async def record(frame: object, direction: object = None) -> None:
        pushed.append(frame)

    service.push_frame = record  # type: ignore[assignment]

    title = await service.complete_once(messages=({"role": "user", "content": "命名這一輪"},))

    assert title == "週末的旅行計畫"
    # Nothing was pushed downstream, so the sentence gate and TTS cannot see it,
    # and the proposal tools are not offered for work that is not a turn.
    assert pushed == []
    assert host.tools == ()
    assert host.max_tokens == 32
    assert host.messages == ({"role": "user", "content": "命名這一輪"},)


@pytest.mark.asyncio
async def test_a_cancelled_or_faulted_completion_answers_with_nothing() -> None:
    cancelled = build_service(FakeWorkerHost(chunks=("一半",), status="cancelled"))
    assert await cancelled.complete_once(messages=({"role": "user", "content": "命名"},)) == ""

    faulted = build_service(FakeWorkerHost(error=WorkerError("worker_eof")))
    assert await faulted.complete_once(messages=({"role": "user", "content": "命名"},)) == ""


@pytest.mark.asyncio
async def test_an_arriving_turn_takes_the_worker_back_from_background_work() -> None:
    host = TwoPhaseWorkerHost()
    service = LocalQwenLLMService(
        host=host,  # type: ignore[arg-type]
        system_instruction="private persona",
    )
    provider = PipecatAttemptProvider(model="qwen3.5-4b-q4-local", service=service)
    background = asyncio.create_task(
        service.complete_once(messages=({"role": "user", "content": "命名這一輪"},))
    )
    try:
        await asyncio.wait_for(host.started.wait(), timeout=2.0)
        frames = await collect(provider)
    finally:
        await provider.close()

    # The turn did not queue behind the title: it stopped it, then ran.
    assert host.cancelled == [1]
    assert host.generations == [1, 2]
    assert await asyncio.wait_for(background, timeout=2.0) == ""
    assert [frame.text for frame in frames if isinstance(frame, GenerationLLMTextFrame)] == ["好。"]
    terminal = frames[-1]
    assert isinstance(terminal, ProviderTerminalFrame)
    assert terminal.status == "completed"


def test_a_completion_needs_a_message_and_respects_the_output_bound() -> None:
    service = build_service(FakeWorkerHost())
    with pytest.raises(ValueError):
        asyncio.run(service.complete_once(messages=()))
    with pytest.raises(ValueError):
        asyncio.run(
            service.complete_once(messages=({"role": "user", "content": "命名"},), max_tokens=193)
        )
