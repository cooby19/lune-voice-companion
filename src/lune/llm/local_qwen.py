"""Expose the isolated on-device Qwen worker as an ordinary Pipecat ``LLMService``.

The pipeline stays provider-neutral: ``PipecatAttemptProvider`` still sees Pipecat text,
function-call, metrics and terminal frames, so generation fencing, the three-sentence gate,
the two-stage proposal path and cancel/drain semantics are unchanged. Only the source of
the tokens differs.

Nothing here relaxes the worker's boundary. The weights stay in a separate process with an
allowlisted, forced-offline environment, thinking text is filtered before it can reach the
sentence gate, and ``<tool_call>`` blocks are lifted out of the stream rather than spoken.

One worker serves one generation at a time, so the out-of-band :meth:`complete_once` (the
thread title) shares this service's lock and always yields to an arriving turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, Final
from uuid import uuid4

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    FunctionCallInProgressFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    MetricsFrame,
)
from pipecat.metrics.metrics import LLMTokenUsage, LLMUsageMetricsData
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService

from lune.llm_spike.tools import AFFINITY_TOOL, MEMORY_TOOL
from lune.llm_spike.worker import QwenWorkerHost, WorkerError

LOCAL_TOOLS: Final[tuple[Mapping[str, Any], ...]] = (MEMORY_TOOL, AFFINITY_TOOL)

# The worker speaks the chat-template roles. Pipecat's "developer" role carries the
# locally selected summary and memories, which the template has no slot for.
_ROLE_MAP: Final[Mapping[str, str]] = {"developer": "system", "system": "system"}


class LocalQwenLLMService(LLMService):
    """Drive one worker generation per context frame and stream its visible text."""

    def __init__(
        self,
        *,
        host: QwenWorkerHost,
        system_instruction: str,
        max_output_tokens: int = 192,
        tools: Sequence[Mapping[str, Any]] = LOCAL_TOOLS,
    ) -> None:
        if not system_instruction:
            raise ValueError("a private persona instruction is required")
        if not 1 <= max_output_tokens <= 192:
            raise ValueError("M3 output limit must be between one and 192 tokens")
        super().__init__()
        self._host = host
        self._system_instruction = system_instruction
        self._max_output_tokens = max_output_tokens
        self._tools = tuple(tools)
        self._started = False
        self._active_generation: int | None = None
        self._background_generation: int | None = None
        self._worker_lock = asyncio.Lock()

    async def preload(self) -> None:
        """Load the weights before the first turn so it is not charged the load time."""

        if self._started:
            return
        await self._host.start()
        self._started = True

    async def close(self) -> None:
        self._started = False
        await self._host.close()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            # System frames run on their own task, so this arrives while a
            # generation is still awaiting the worker.
            await self._cancel_active()
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, LLMContextFrame):
            await self._generate(frame.context)
            return
        await self.push_frame(frame, direction)

    async def complete_once(
        self,
        *,
        messages: Sequence[Mapping[str, str]],
        max_tokens: int = 32,
    ) -> str:
        """Answer one off-path question on the loaded worker, pushing no frames.

        This exists so features that need a sentence of local text -- naming a
        thread, today -- can reuse the weights the turn path already paid for
        instead of opening a request of their own.  Nothing is pushed
        downstream, so the sentence gate and TTS never see it, and the turn path
        outranks it: an arriving generation cancels this one first, and an
        interruption cancels it like any other.  A cancelled or failed
        completion returns an empty string rather than raising, because its
        caller must never be able to fail a conversation.
        """

        if not messages:
            raise ValueError("a local completion needs at least one message")
        if not 1 <= max_tokens <= 192:
            raise ValueError("M3 output limit must be between one and 192 tokens")
        async with self._worker_lock:
            await self.preload()
            generation_id = self._host.advance_generation()
            self._active_generation = generation_id
            self._background_generation = generation_id
            try:
                outcome = await self._host.generate(
                    generation_id=generation_id,
                    request_id=uuid4().hex,
                    messages=messages,
                    max_tokens=max_tokens,
                )
            except asyncio.CancelledError:
                await self._cancel_active()
                raise
            except WorkerError:
                return ""
            finally:
                self._active_generation = None
                self._background_generation = None
        return outcome.text if outcome.status == "completed" else ""

    async def _generate(self, context: LLMContext) -> None:
        # A turn never queues behind background work: it stops it first, then
        # takes the worker as soon as the cancel is acknowledged.
        await self._preempt_background()
        async with self._worker_lock:
            await self._generate_turn(context)

    async def _generate_turn(self, context: LLMContext) -> None:
        await self.preload()
        generation_id = self._host.advance_generation()
        self._active_generation = generation_id
        await self.push_frame(LLMFullResponseStartFrame())
        try:
            outcome = await self._host.generate(
                generation_id=generation_id,
                request_id=uuid4().hex,
                messages=_worker_messages(context, self._system_instruction),
                tools=self._tools,
                max_tokens=self._max_output_tokens,
                on_text=self._push_text,
            )
        except asyncio.CancelledError:
            await self._cancel_active()
            raise
        except WorkerError as error:
            # A worker fault is transient by construction: the host can respawn it,
            # and nothing was charged, so the turn may be retried.
            await self.push_frame(ErrorFrame(error=f"local_qwen_{error.code}", fatal=False))
            return
        finally:
            self._active_generation = None

        if outcome.status == "cancelled":
            # The fence owns cancellation; emitting a terminal frame here would
            # race the provider's own cancelled verdict.
            return
        if outcome.status == "error":
            await self.push_frame(ErrorFrame(error="local_qwen_generation_failed", fatal=False))
            return
        for call in outcome.tool_calls:
            if call.malformed:
                continue
            await self.push_frame(
                FunctionCallInProgressFrame(
                    function_name=call.tool_name,
                    tool_call_id=uuid4().hex,
                    arguments=call.arguments_json,
                )
            )
        await self.push_frame(
            _usage_metrics(self, outcome.prompt_tokens, outcome.generation_tokens)
        )
        await self.push_frame(LLMFullResponseEndFrame())

    async def _push_text(self, text: str) -> None:
        await self.push_frame(LLMTextFrame(text=text))

    async def _preempt_background(self) -> None:
        """Stop an out-of-band completion so a real turn is not queued behind it."""

        if self._background_generation is not None:
            await self._cancel_active()

    async def _cancel_active(self) -> None:
        generation_id = self._active_generation
        if generation_id is None:
            return
        try:
            await self._host.cancel(generation_id)
        except WorkerError:
            # The worker is already gone, which is the outcome cancel wanted.
            return


def _worker_messages(context: LLMContext, system_instruction: str) -> tuple[dict[str, str], ...]:
    """Flatten the bounded context into chat-template roles the worker accepts."""

    messages: list[dict[str, str]] = [{"role": "system", "content": system_instruction}]
    for message in context.get_messages():
        if not isinstance(message, dict):
            # A provider-specific message carries no portable role/content pair,
            # and this context is only ever built from plain dicts.
            continue
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        if not isinstance(content, str) or not content:
            continue
        messages.append({"role": _ROLE_MAP.get(role, role), "content": content})
    return tuple(messages)


def _usage_metrics(
    service: LLMService,
    prompt_tokens: int | None,
    generation_tokens: int | None,
) -> MetricsFrame:
    """Report token counts honestly; on-device tokens are counted but never priced."""

    prompt = max(0, prompt_tokens or 0)
    completion = max(0, generation_tokens or 0)
    return MetricsFrame(
        data=[
            LLMUsageMetricsData(
                processor=service.name,
                value=LLMTokenUsage(
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                    total_tokens=prompt + completion,
                ),
            )
        ]
    )
