"""The single entry point that invalidates a generation across the whole pipeline."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Final, Protocol

from lune.diagnostics import SafeDiagnostics
from lune.pipeline.contracts import CancelEvent, CancelReason, CancelStage

_HISTORY_LIMIT: Final[int] = 64


class PlaybackFence(Protocol):
    async def stop_generation(self, generation_id: int) -> None: ...


class TTSFence(Protocol):
    async def cancel(self, generation_id: int) -> None: ...


class STTFence(Protocol):
    def set_generation(self, generation_id: int) -> None: ...


class ProviderFence(Protocol):
    """Broadcast a Pipecat interruption and drain whatever the provider still owes."""

    async def interrupt_and_drain(self, generation_id: int) -> None: ...


class ProposalFence(Protocol):
    def cancel_generation(self, generation_id: int) -> int: ...


class TurnGateFence(Protocol):
    def reset_generation(self, generation_id: int) -> None: ...

    def carry_over_generation(self, generation_id: int) -> None: ...


class TransportFence(Protocol):
    def set_generation(self, generation_id: int) -> None: ...

    def rebuild(self, *, generation_id: int) -> None: ...


class GenerationCoordinator:
    """Own the generation counter so no component can cancel behind another's back.

    Cancelling always advances the fence first and synchronously, so every frame,
    tool call and PCM chunk still in flight is stale the moment this returns to
    its caller. Only then are the collaborators torn down, audible output first:
    the ``audible_stop_ms`` this reports is the value the 200 ms barge-in gate
    measures.
    """

    def __init__(
        self,
        *,
        playback: PlaybackFence,
        tts: TTSFence,
        stt: STTFence,
        turn_gate: TurnGateFence,
        proposals: ProposalFence,
        provider: ProviderFence | None = None,
        transport: TransportFence | None = None,
        initial_generation: int = 0,
        diagnostics: SafeDiagnostics | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if initial_generation < 0:
            raise ValueError("generation ID cannot be negative")
        self._playback = playback
        self._tts = tts
        self._stt = stt
        self._turn_gate = turn_gate
        self._proposals = proposals
        self._provider = provider
        self._transport = transport
        self._generation_id = initial_generation
        self._diagnostics = diagnostics
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._history: deque[CancelEvent] = deque(maxlen=_HISTORY_LIMIT)

    @property
    def generation_id(self) -> int:
        return self._generation_id

    @property
    def cancel_events(self) -> tuple[CancelEvent, ...]:
        """The most recent cancellations, newest last, for gates and diagnostics."""

        return tuple(self._history)

    def is_current(self, generation_id: int) -> bool:
        return generation_id == self._generation_id

    async def cancel(self, reason: CancelReason) -> CancelEvent:
        """Advance the fence immediately, then tear down in a fixed order."""

        previous = self._generation_id
        current = previous + 1
        self._generation_id = current
        async with self._lock:
            failed: list[CancelStage] = []
            started = self._monotonic()
            await self._run(failed, "playback", lambda: self._playback.stop_generation(previous))
            await self._run(failed, "tts", lambda: self._tts.cancel(previous))
            audible_stop_ms = (self._monotonic() - started) * 1_000.0

            self._run_sync(failed, "stt", lambda: self._stt.set_generation(current))
            if self._provider is not None:
                provider = self._provider
                await self._run(failed, "provider", lambda: provider.interrupt_and_drain(previous))
            self._run_sync(failed, "proposals", lambda: self._proposals.cancel_generation(previous))
            # The gate must adopt the new fence before the transport re-stamps
            # incoming PCM, otherwise barge-in speech is dropped between the two.
            self._run_sync(failed, "turn_gate", lambda: self._fence_turn_gate(reason, current))
            self._run_sync(failed, "transport", lambda: self._fence_transport(reason, current))

        event = CancelEvent(
            previous_generation_id=previous,
            generation_id=current,
            reason=reason,
            audible_stop_ms=audible_stop_ms,
            failed_stages=tuple(failed),
        )
        self._history.append(event)
        self._emit(event)
        return event

    def _fence_turn_gate(self, reason: CancelReason, generation_id: int) -> None:
        if reason == "barge_in":
            # The speech that caused the interruption is the next utterance, so it
            # is re-stamped rather than discarded.
            self._turn_gate.carry_over_generation(generation_id)
            return
        self._turn_gate.reset_generation(generation_id)

    def _fence_transport(self, reason: CancelReason, generation_id: int) -> None:
        if self._transport is None:
            return
        if reason in ("device_changed", "output_overflow"):
            self._transport.rebuild(generation_id=generation_id)
            return
        self._transport.set_generation(generation_id)

    async def _run(
        self,
        failed: list[CancelStage],
        stage: CancelStage,
        call: Callable[[], Awaitable[None]],
    ) -> None:
        try:
            await call()
        except asyncio.CancelledError:
            raise
        except Exception:
            failed.append(stage)

    def _run_sync(
        self,
        failed: list[CancelStage],
        stage: CancelStage,
        call: Callable[[], object],
    ) -> None:
        try:
            call()
        except Exception:
            failed.append(stage)

    def _emit(self, event: CancelEvent) -> None:
        if self._diagnostics is None:
            return
        self._diagnostics.emit(
            event=f"cancel_{event.reason}",
            generation_id=event.generation_id,
            duration_ms=round(event.audible_stop_ms, 3),
            count=len(event.failed_stages),
        )
