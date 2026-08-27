"""Generation-fenced provider attempts, fallback, and cancel/drain orchestration."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from lune.llm.budget import AttemptReservation, BudgetLedger, BudgetLocked
from lune.llm.contracts import (
    AttemptUsageFrame,
    GenerationFunctionCallFrame,
    GenerationLLMTextFrame,
    ModelName,
    ProviderErrorCode,
    ProviderStreamFrame,
    ProviderTerminalFrame,
)
from lune.llm.prompt import PromptContext
from lune.llm.sentence_gate import SentenceGate

type StreamFrameFactory = Callable[[int, str], ProviderStreamFrame]
type FrameSink = Callable[[ProviderStreamFrame], Awaitable[None]]
type GenerationStatus = Literal["completed", "cancelled", "error", "budget_locked"]
type GenerationErrorCode = ProviderErrorCode | Literal["budget_locked"]


class AttemptStreamProvider(Protocol):
    model: ModelName

    def stream(
        self,
        *,
        generation_id: int,
        attempt_id: str,
        context: PromptContext,
    ) -> AsyncIterator[ProviderStreamFrame]: ...

    async def cancel_and_drain(
        self,
        *,
        generation_id: int,
        attempt_id: str,
    ) -> tuple[ProviderStreamFrame, ...]: ...


@dataclass(frozen=True, slots=True)
class GenerationResult:
    status: GenerationStatus
    models_attempted: tuple[ModelName, ...]
    sentences_emitted: int
    error_code: GenerationErrorCode | None = None


@dataclass(slots=True)
class ScriptedAttemptProvider:
    """Deterministic async provider used by public tests without API access."""

    model: ModelName
    scripts: Sequence[Sequence[StreamFrameFactory]] = field(repr=False)
    drains: Sequence[Sequence[StreamFrameFactory]] = field(default=(), repr=False)
    _scripts: deque[tuple[StreamFrameFactory, ...]] = field(init=False, repr=False)
    _drains: deque[tuple[StreamFrameFactory, ...]] = field(init=False, repr=False)
    cancelled_attempts: list[str] = field(init=False)
    drained_attempts: list[str] = field(init=False)

    def __post_init__(self) -> None:
        self._scripts = deque(tuple(script) for script in self.scripts)
        self._drains = deque(tuple(script) for script in self.drains)
        self.cancelled_attempts: list[str] = []
        self.drained_attempts: list[str] = []

    async def stream(
        self,
        *,
        generation_id: int,
        attempt_id: str,
        context: PromptContext,
    ) -> AsyncIterator[ProviderStreamFrame]:
        del context
        script = self._scripts.popleft()
        for factory in script:
            await asyncio.sleep(0)
            yield factory(generation_id, attempt_id)

    async def cancel_and_drain(
        self,
        *,
        generation_id: int,
        attempt_id: str,
    ) -> tuple[ProviderStreamFrame, ...]:
        self.cancelled_attempts.append(attempt_id)
        self.drained_attempts.append(attempt_id)
        if not self._drains:
            return ()
        await asyncio.sleep(0)
        return tuple(factory(generation_id, attempt_id) for factory in self._drains.popleft())


class ConversationGenerator:
    """Run one bounded response and retry Luna only before the first playback frame."""

    def __init__(
        self,
        *,
        providers: Mapping[ModelName, AttemptStreamProvider],
        ledger: BudgetLedger,
        current_generation: Callable[[], int],
        emit: FrameSink,
        local_error_text: str = "抱歉\uff0c雲端回覆暫時中斷了。",
    ) -> None:
        self._providers = providers
        self._ledger = ledger
        self._current_generation = current_generation
        self._emit = emit
        self._local_error_text = local_error_text

    async def generate(
        self,
        *,
        generation_id: int,
        context: PromptContext,
        at: datetime,
        max_input_tokens: int,
        max_output_tokens: int = 192,
    ) -> GenerationResult:
        try:
            reservation = self._ledger.reserve_conversation(
                at=at,
                max_input_tokens=max_input_tokens,
                max_output_tokens=max_output_tokens,
            )
        except BudgetLocked:
            return GenerationResult("budget_locked", (), 0, "budget_locked")

        attempted: list[ModelName] = []
        sentences_emitted = 0
        while True:
            attempted.append(reservation.model)
            attempt = await self._run_attempt(
                generation_id=generation_id,
                context=context,
                reservation=reservation,
            )
            sentences_emitted += attempt.sentences_emitted
            if (
                attempt.retry_luna
                and reservation.model == "gpt-5.6-terra"
                and self._current_generation() == generation_id
            ):
                try:
                    reservation = self._ledger.reserve_model(
                        at=at,
                        model="gpt-5.6-luna",
                        max_input_tokens=max_input_tokens,
                        max_output_tokens=max_output_tokens,
                    )
                except BudgetLocked:
                    return GenerationResult(
                        "budget_locked",
                        tuple(attempted),
                        sentences_emitted,
                        "budget_locked",
                    )
                continue
            return GenerationResult(
                attempt.status,
                tuple(attempted),
                sentences_emitted,
                attempt.error_code,
            )

    async def _run_attempt(
        self,
        *,
        generation_id: int,
        context: PromptContext,
        reservation: AttemptReservation,
    ) -> _AttemptResult:
        provider = self._providers[reservation.model]
        gate = SentenceGate(max_sentences=3)
        usage: AttemptUsageFrame | None = None
        terminal: ProviderTerminalFrame | None = None
        playback_started = False

        async for frame in provider.stream(
            generation_id=generation_id,
            attempt_id=reservation.attempt_id,
            context=context,
        ):
            if not _belongs_to(frame, generation_id, reservation.attempt_id):
                continue
            if self._current_generation() != generation_id:
                usage = await self._cancel_drain_usage(provider, reservation, generation_id, usage)
                self._ledger.settle(reservation.attempt_id, usage)
                return _AttemptResult("cancelled", gate.released_sentences)
            if isinstance(frame, AttemptUsageFrame):
                usage = frame
                continue
            if terminal is not None:
                continue
            if isinstance(frame, GenerationLLMTextFrame):
                result = gate.feed(frame)
                for released in result.frames:
                    await self._emit(released)
                    playback_started = True
                if result.reached_limit:
                    usage = await self._cancel_drain_usage(
                        provider, reservation, generation_id, usage
                    )
                    self._ledger.settle(reservation.attempt_id, usage)
                    return _AttemptResult("completed", gate.released_sentences)
            elif isinstance(frame, GenerationFunctionCallFrame):
                await self._emit(frame)
            elif isinstance(frame, ProviderTerminalFrame):
                terminal = frame

        if terminal is None:
            terminal = ProviderTerminalFrame(
                generation_id=generation_id,
                attempt_id=reservation.attempt_id,
                status="incomplete",
                transient=True,
                error_code="stream_incomplete",
            )

        if terminal.status == "completed":
            for released in gate.finish().frames:
                await self._emit(released)
                playback_started = True
            self._ledger.settle(reservation.attempt_id, usage)
            return _AttemptResult("completed", gate.released_sentences)

        gate.discard()
        self._ledger.settle(reservation.attempt_id, usage)
        retry_luna = terminal.transient and not playback_started
        if playback_started:
            await self._emit(
                GenerationLLMTextFrame(
                    text=self._local_error_text,
                    generation_id=generation_id,
                    attempt_id=reservation.attempt_id,
                )
            )
            return _AttemptResult("error", gate.released_sentences + 1, terminal.error_code)
        return _AttemptResult(
            "error",
            gate.released_sentences,
            terminal.error_code,
            retry_luna=retry_luna,
        )

    async def _cancel_drain_usage(
        self,
        provider: AttemptStreamProvider,
        reservation: AttemptReservation,
        generation_id: int,
        current: AttemptUsageFrame | None,
    ) -> AttemptUsageFrame | None:
        drained = await provider.cancel_and_drain(
            generation_id=generation_id,
            attempt_id=reservation.attempt_id,
        )
        usage = current
        for frame in drained:
            if isinstance(frame, AttemptUsageFrame) and _belongs_to(
                frame, generation_id, reservation.attempt_id
            ):
                usage = frame
        return usage


@dataclass(frozen=True, slots=True)
class _AttemptResult:
    status: GenerationStatus
    sentences_emitted: int
    error_code: ProviderErrorCode | None = None
    retry_luna: bool = False


def _belongs_to(frame: ProviderStreamFrame, generation_id: int, attempt_id: str) -> bool:
    return frame.generation_id == generation_id and frame.attempt_id == attempt_id
