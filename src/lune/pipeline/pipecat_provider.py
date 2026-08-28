"""Adapt a Pipecat ``LLMService`` to Lune's typed, generation-fenced attempts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Final

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    FunctionCallInProgressFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    MetricsFrame,
)
from pipecat.metrics.metrics import LLMUsageMetricsData
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import LLMService
from pipecat.utils.asyncio.task_manager import TaskManager
from pipecat.workers.base_worker import WorkerParams

from lune.llm.contracts import (
    AttemptUsageFrame,
    GenerationFunctionCallFrame,
    GenerationLLMTextFrame,
    ModelName,
    ProviderStreamFrame,
    ProviderTerminalFrame,
)
from lune.llm.prompt import PromptContext

_DRAIN_STASH_LIMIT: Final[int] = 4


@dataclass(frozen=True, slots=True)
class _Tagged:
    """One pipeline frame bound to the attempt that was live when it arrived."""

    generation_id: int
    attempt_id: str
    frame: Frame | None = field(default=None, repr=False)
    cancelled: bool = False


class _AttemptCollector(FrameProcessor):
    """Terminal processor that tags every downstream frame with its attempt."""

    def __init__(self, provider: PipecatAttemptProvider) -> None:
        super().__init__()
        self._provider = provider

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        self._provider.collect(frame)
        await self.push_frame(frame, direction)


class PipecatAttemptProvider:
    """Run one long-lived Pipecat worker and expose it as typed attempt streams.

    Only one attempt is in flight at a time, so frames are tagged with the live
    attempt as they leave the service. Cancelling broadcasts a Pipecat
    ``InterruptionFrame``: that is what makes the Responses WebSocket send
    ``response.cancel`` instead of merely closing the client stream, so the
    ``remote_cancel`` capability stays honest.
    """

    def __init__(
        self,
        *,
        model: ModelName,
        service: LLMService,
        queue_capacity: int = 512,
        stall_timeout_s: float = 15.0,
        drain_timeout_s: float = 0.25,
        ready_timeout_s: float = 10.0,
    ) -> None:
        if queue_capacity < 1:
            raise ValueError("frame queue capacity must be positive")
        if stall_timeout_s <= 0 or drain_timeout_s <= 0 or ready_timeout_s <= 0:
            raise ValueError("provider timeouts must be positive")
        self.model = model
        self._service = service
        self._queue: asyncio.Queue[_Tagged] = asyncio.Queue(maxsize=queue_capacity)
        self._stall_timeout_s = stall_timeout_s
        self._drain_timeout_s = drain_timeout_s
        self._ready_timeout_s = ready_timeout_s
        self._generation_id = 0
        self._attempt_id = ""
        self._worker: PipelineWorker | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._stash: dict[str, tuple[ProviderStreamFrame, ...]] = {}
        self._stash_order: list[str] = []
        self._stream_finished = asyncio.Event()
        self._stream_finished.set()
        self._closed = False

    @property
    def active_attempt_id(self) -> str:
        return self._attempt_id

    def collect(self, frame: Frame) -> None:
        """Called from the pipeline task; never blocks and never drops silently."""

        self._offer(_Tagged(self._generation_id, self._attempt_id, frame))

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("provider is closed")
        if self._worker is not None:
            return
        worker = PipelineWorker(
            Pipeline([self._service, _AttemptCollector(self)]),
            params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
            enable_rtvi=False,
            enable_turn_tracking=False,
            enable_tracing=False,
            idle_timeout_secs=None,
            check_dangling_tasks=False,
        )
        worker.event_handler("on_pipeline_started")(self._on_started)
        worker.event_handler("on_pipeline_error")(self._on_error)
        self._worker = worker
        loop = asyncio.get_running_loop()
        params = WorkerParams(task_manager=TaskManager(loop=loop))
        self._run_task = asyncio.create_task(worker.run(params), name=f"lune-llm-{self.model}")
        await asyncio.wait_for(self._ready.wait(), timeout=self._ready_timeout_s)

    async def stream(
        self,
        *,
        generation_id: int,
        attempt_id: str,
        context: PromptContext,
    ) -> AsyncIterator[ProviderStreamFrame]:
        await self.start()
        worker = self._worker
        assert worker is not None
        self._begin_attempt(generation_id, attempt_id)
        await worker.queue_frame(LLMContextFrame(context=context.to_pipecat()))
        try:
            while True:
                try:
                    timeout = self._stall_timeout_s
                    tagged = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                except TimeoutError:
                    yield ProviderTerminalFrame(
                        generation_id=generation_id,
                        attempt_id=attempt_id,
                        status="incomplete",
                        transient=True,
                        error_code="stream_incomplete",
                    )
                    return
                if tagged.attempt_id != attempt_id or tagged.generation_id != generation_id:
                    continue
                if tagged.cancelled:
                    yield ProviderTerminalFrame(
                        generation_id=generation_id,
                        attempt_id=attempt_id,
                        status="cancelled",
                        error_code="cancelled",
                    )
                    return
                assert tagged.frame is not None
                for mapped in _map_frame(tagged.frame, generation_id, attempt_id):
                    yield mapped
                    if isinstance(mapped, ProviderTerminalFrame):
                        return
        finally:
            self._stream_finished.set()

    async def cancel_and_drain(
        self,
        *,
        generation_id: int,
        attempt_id: str,
    ) -> tuple[ProviderStreamFrame, ...]:
        stashed = self._stash.pop(attempt_id, None)
        if stashed is not None:
            if attempt_id in self._stash_order:
                self._stash_order.remove(attempt_id)
            return stashed
        # Called from inside the stream consumer, which is suspended awaiting
        # this call, so the queue has no competing reader and can drain at once.
        drained = await self._interrupt(
            generation_id=generation_id,
            attempt_id=attempt_id,
            wait_for_stream=False,
        )
        return drained or ()

    async def interrupt_and_drain(self, generation_id: int) -> None:
        """The coordinator's fence hook: stop this generation and keep its usage."""

        attempt_id = self._attempt_id
        if not attempt_id or self._generation_id != generation_id:
            return
        drained = await self._interrupt(
            generation_id=generation_id,
            attempt_id=attempt_id,
            wait_for_stream=True,
        )
        if drained is None:
            # The stream is still reading; leave the drain to its own call so the
            # attempt's usage is recorded exactly once.
            return
        self._stash[attempt_id] = drained
        self._stash_order.append(attempt_id)
        while len(self._stash_order) > _DRAIN_STASH_LIMIT:
            self._stash.pop(self._stash_order.pop(0), None)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        worker = self._worker
        self._worker = None
        run_task = self._run_task
        self._run_task = None
        if worker is not None:
            await worker.cancel()
        if run_task is not None and not run_task.done():
            try:
                await asyncio.wait_for(run_task, timeout=self._ready_timeout_s)
            except (TimeoutError, asyncio.CancelledError):
                run_task.cancel()

    def _begin_attempt(self, generation_id: int, attempt_id: str) -> None:
        self._generation_id = generation_id
        self._attempt_id = attempt_id
        self._stream_finished.clear()
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _interrupt(
        self,
        *,
        generation_id: int,
        attempt_id: str,
        wait_for_stream: bool,
    ) -> tuple[ProviderStreamFrame, ...] | None:
        worker = self._worker
        if worker is not None:
            await worker.queue_frame(InterruptionFrame())
        # Unblock the in-flight stream so the fence check runs immediately
        # instead of waiting for the provider's own stall timeout, then let it
        # retire before draining: two readers on one queue would race.
        self._offer(_Tagged(generation_id, attempt_id, cancelled=True))
        if wait_for_stream:
            try:
                await asyncio.wait_for(self._stream_finished.wait(), timeout=self._drain_timeout_s)
            except TimeoutError:
                return None
        drained: list[ProviderStreamFrame] = []
        deadline = asyncio.get_running_loop().time() + self._drain_timeout_s
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                tagged = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except TimeoutError:
                break
            if tagged.cancelled or tagged.frame is None:
                continue
            if tagged.attempt_id != attempt_id or tagged.generation_id != generation_id:
                continue
            drained.extend(_map_frame(tagged.frame, generation_id, attempt_id))
        return tuple(drained)

    def _offer(self, tagged: _Tagged) -> None:
        if not tagged.attempt_id:
            return
        try:
            self._queue.put_nowait(tagged)
        except asyncio.QueueFull:
            # A full queue means the consumer is gone; dropping is the only
            # bounded option and the stream's stall timeout still terminates it.
            pass

    async def _on_started(self, worker: PipelineWorker, frame: Frame) -> None:
        del worker, frame
        self._ready.set()

    async def _on_error(self, worker: PipelineWorker, frame: ErrorFrame) -> None:
        del worker
        self._offer(_Tagged(self._generation_id, self._attempt_id, frame))


def _map_frame(
    frame: Frame,
    generation_id: int,
    attempt_id: str,
) -> tuple[ProviderStreamFrame, ...]:
    """Translate one pipeline frame; anything unmapped is deliberately ignored.

    ``LLMThoughtTextFrame`` is not an ``LLMTextFrame`` in Pipecat 1.7, so
    reasoning text cannot reach the sentence gate through this mapping.
    """

    if isinstance(frame, LLMTextFrame):
        if not frame.text:
            return ()
        return (
            GenerationLLMTextFrame(
                text=frame.text,
                generation_id=generation_id,
                attempt_id=attempt_id,
            ),
        )
    if isinstance(frame, FunctionCallInProgressFrame):
        return (
            GenerationFunctionCallFrame(
                function_name=frame.function_name,
                tool_call_id=frame.tool_call_id,
                arguments=frame.arguments,
                generation_id=generation_id,
                attempt_id=attempt_id,
            ),
        )
    if isinstance(frame, MetricsFrame):
        usage = _usage_frame(frame, generation_id, attempt_id)
        return () if usage is None else (usage,)
    if isinstance(frame, ErrorFrame):
        return (
            ProviderTerminalFrame(
                generation_id=generation_id,
                attempt_id=attempt_id,
                status="failed",
                transient=not frame.fatal,
                error_code="provider_error",
            ),
        )
    if isinstance(frame, LLMFullResponseEndFrame):
        return (
            ProviderTerminalFrame(
                generation_id=generation_id,
                attempt_id=attempt_id,
                status="completed",
            ),
        )
    return ()


def _usage_frame(
    frame: MetricsFrame,
    generation_id: int,
    attempt_id: str,
) -> AttemptUsageFrame | None:
    for item in frame.data:
        if not isinstance(item, LLMUsageMetricsData):
            continue
        tokens: Any = item.value
        input_tokens = max(0, int(tokens.prompt_tokens))
        cached = _clamp(tokens.cache_read_input_tokens, input_tokens)
        written = _clamp(tokens.cache_creation_input_tokens, input_tokens - cached)
        return AttemptUsageFrame(
            generation_id=generation_id,
            attempt_id=attempt_id,
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            cache_write_input_tokens=written,
            output_tokens=max(0, int(tokens.completion_tokens)),
        )
    return None


def _clamp(value: int | None, ceiling: int) -> int:
    """Clamp a reported cache count down, which can only raise the charged cost."""

    return max(0, min(int(value or 0), max(0, ceiling)))
