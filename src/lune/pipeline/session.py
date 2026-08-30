"""The fixed M6 turn path from captured speech to confirmed playback."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from lune.audio.devices import DeviceSnapshot, DeviceStateMachine, MaybeAwaitable
from lune.audio.types import AudioSpan
from lune.diagnostics import SafeDiagnostics
from lune.llm.budget import BudgetLedger
from lune.llm.contracts import (
    GenerationFunctionCallFrame,
    GenerationLLMTextFrame,
    ModelName,
    ProviderStreamFrame,
)
from lune.llm.prompt import PromptContext
from lune.llm.streaming import AttemptStreamProvider, ConversationGenerator, GenerationResult
from lune.llm_spike.tools import PROPOSE_AFFINITY, PROPOSE_MEMORY, ToolCallValidator
from lune.memory.proposals import AffinityProposal, MemoryProposal, ProposalHost
from lune.memory.store import MemoryStore
from lune.memory.summary import RollingSummaryManager
from lune.memory.titles import ThreadTitleManager
from lune.pipeline.contracts import TurnGateEvent, TurnOutcome, TurnStarted, UtteranceCaptured
from lune.pipeline.coordinator import GenerationCoordinator, ProviderFence
from lune.pipeline.enricher import ContextEnricher
from lune.pipeline.playback import PlaybackSink
from lune.pipeline.turn_gate import VoiceTurnGate
from lune.readiness import AppState
from lune.stt.contracts import FinalTranscript, STTEvent, STTFailure, TranscriptionRequest
from lune.tts.contracts import TTSBackendError, TTSRequest
from lune.tts.router import TTSRouterService

CHARACTERS_PER_TOKEN = 1.0
"""One token per character is deliberately pessimistic for both Chinese and English."""
INPUT_TOKEN_MARGIN = 1.25
MIN_INPUT_TOKENS = 256
_REPORT_LIMIT = 256
_GENERATION_SCOPED_STATES: frozenset[str] = frozenset({"thinking", "speaking"})
"""States that describe one generation's work and expire when it is cancelled."""


class SampleClock(Protocol):
    """Maps a captured sample offset back to wall-clock time."""

    def wall_time_of_sample(self, sample: int) -> float | None: ...


class FinalOnlySTT(Protocol):
    def set_generation(self, generation_id: int) -> None: ...

    def submit(self, request: TranscriptionRequest) -> bool: ...

    async def close(self) -> None: ...


class ProviderFenceGroup:
    """Fan the coordinator's provider fence out to every configured model."""

    def __init__(self, fences: Sequence[ProviderFence]) -> None:
        self._fences = tuple(fences)

    async def interrupt_and_drain(self, generation_id: int) -> None:
        for fence in self._fences:
            await fence.interrupt_and_drain(generation_id)


@dataclass(slots=True)
class _ActiveTurn:
    generation_id: int
    turn_id: str
    validator: ToolCallValidator = field(repr=False)
    speak_text: bool = True
    spoke: bool = False
    degraded: bool = False
    failed: bool = False
    played_sentences: int = 0
    rejected_tool_calls: int = 0


@dataclass(frozen=True, slots=True)
class TurnReport:
    """What one turn actually did, with no transcript or generated text."""

    generation_id: int
    outcome: TurnOutcome
    models_attempted: tuple[ModelName, ...]
    sentences_played: int
    degraded_tts: bool
    rejected_tool_calls: int


class VoiceSession:
    """Own one conversation: gate, STT, context, provider, sentence gate, TTS, output.

    Every stage is fenced by the same generation counter, and the coordinator is
    the only writer of that counter. A stage that observes a moved fence stops
    without writing to the database, so a cancelled turn leaves no transcript,
    no assistant text and no memory proposal behind.
    """

    def __init__(
        self,
        *,
        session_id: str,
        store: MemoryStore,
        coordinator: GenerationCoordinator,
        turn_gate: VoiceTurnGate,
        stt: FinalOnlySTT,
        enricher: ContextEnricher,
        providers: Mapping[ModelName, AttemptStreamProvider],
        ledger: BudgetLedger,
        tts: TTSRouterService,
        playback: PlaybackSink,
        proposals: ProposalHost,
        summarizer: RollingSummaryManager | None = None,
        titler: ThreadTitleManager | None = None,
        rebuild_streams: Callable[[DeviceSnapshot], MaybeAwaitable] | None = None,
        primary_model: ModelName | None = None,
        max_output_tokens: int = 192,
        max_input_tokens: int = 4_096,
        stt_timeout_s: float = 10.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        sample_clock: SampleClock | None = None,
        diagnostics: SafeDiagnostics | None = None,
    ) -> None:
        if stt_timeout_s <= 0:
            raise ValueError("the STT watchdog needs a positive timeout")
        if not 1 <= max_output_tokens <= 192:
            raise ValueError("M3 output limit must be between one and 192 tokens")
        self._session_id = session_id
        self._store = store
        self._coordinator = coordinator
        self._turn_gate = turn_gate
        self._stt = stt
        self._enricher = enricher
        self._tts = tts
        self._playback = playback
        self._proposals = proposals
        self._summarizer = summarizer
        self._titler = titler
        self._max_output_tokens = max_output_tokens
        self._max_input_tokens = max_input_tokens
        self._stt_timeout_s = stt_timeout_s
        self._now = now
        self._monotonic = monotonic
        self._sample_clock = sample_clock
        self._diagnostics = diagnostics
        self._generator = ConversationGenerator(
            providers=providers,
            ledger=ledger,
            current_generation=lambda: self._coordinator.generation_id,
            emit=self._on_llm_frame,
            primary_model=primary_model,
        )
        self._devices = DeviceStateMachine(
            cancel_generation=self._cancel_for_devices,
            rebuild_streams=rebuild_streams or (lambda _snapshot: None),
        )
        self._state: AppState = "mic_off"
        self._state_generation = coordinator.generation_id
        self._active_turn: _ActiveTurn | None = None
        self._turn_tasks: set[asyncio.Task[None]] = set()
        self._watchdog: asyncio.Task[None] | None = None
        self._budget_locked = False
        self._degraded_tts = False
        self._reports: deque[TurnReport] = deque(maxlen=_REPORT_LIMIT)
        self._speech_end_at: dict[int, float] = {}
        self._closed = False

    @property
    def state(self) -> AppState:
        """Work states belong to their generation; cancelling one restores idle.

        Without this a cancelled turn could leave the session reporting
        ``speaking`` forever, and the turn policy would keep demanding the
        300 ms barge-in threshold from a user Lune is no longer answering.
        """

        if self._state in _GENERATION_SCOPED_STATES and not self._coordinator.is_current(
            self._state_generation
        ):
            return self._idle_state()
        return self._state

    @property
    def generation_id(self) -> int:
        return self._coordinator.generation_id

    @property
    def budget_locked(self) -> bool:
        return self._budget_locked

    @property
    def degraded_tts(self) -> bool:
        """Sticky for the session: some utterance fell back to the system voice."""

        return self._degraded_tts

    @property
    def output_is_builtin(self) -> bool | None:
        """Return only the safe output category; never expose device identifiers."""

        snapshot = self._devices.snapshot
        return None if snapshot is None else snapshot.output.is_builtin

    @property
    def reports(self) -> tuple[TurnReport, ...]:
        return tuple(self._reports)

    def speech_end_at(self, generation_id: int) -> float | None:
        """When the last voiced sample of this generation's utterance arrived."""

        return self._speech_end_at.get(generation_id)

    async def start(self) -> None:
        await self._playback.start()

    def set_microphone(self, enabled: bool) -> AppState:
        self._devices.set_microphone(enabled)
        self._write_state(self._idle_state())
        return self.state

    async def apply_default_devices(self, snapshot: DeviceSnapshot) -> AppState:
        await self._devices.apply_default_devices(snapshot)
        self._write_state(self._idle_state())
        return self.state

    async def handle_audio(self, span: AudioSpan) -> None:
        """Feed one transport span and act on every event it produces, in order."""

        events = self._turn_gate.feed(span, ai_active=self._ai_active)
        while events:
            for event in events:
                await self._handle_gate_event(event)
            events = (
                self._turn_gate.pump(ai_active=self._ai_active)
                if self._turn_gate.pending_windows
                else ()
            )

    async def on_stt_event(self, event: STTEvent) -> None:
        if not self._coordinator.is_current(event.generation_id):
            # A stale result must not disarm the watchdog guarding a newer turn.
            return
        self._cancel_watchdog()
        if isinstance(event, STTFailure):
            failure: AppState = "setup_required" if event.code == "setup_required" else "error"
            self._set_state(failure, event.generation_id)
            self._emit(event="stt_failed", generation_id=event.generation_id)
            return
        self._schedule_turn(event, speak_text=True)

    async def submit_text(self, text: str, *, speak_text: bool = True) -> AppState:
        """Submit typed input through the same fenced turn path as final STT.

        A typed message during a response is a barge-in.  It advances the
        central generation fence before the new turn is scheduled, but unlike
        spoken barge-in it never carries microphone frames into the next turn.
        """

        clean = text.strip()
        if not clean or len(clean) > 20_000:
            raise ValueError("text input must contain 1 to 20,000 characters")
        if self._closed:
            raise RuntimeError("session is closed")
        if self._ai_active:
            await self._coordinator.cancel("text_barge_in")
        generation_id = self._coordinator.generation_id
        self._set_state("thinking", generation_id)
        self._schedule_turn(
            FinalTranscript(
                request_id=uuid4().hex,
                generation_id=generation_id,
                text=clean,
            ),
            speak_text=speak_text,
        )
        return self.state

    def switch_thread(self, thread_id: str) -> None:
        """Move an idle pipeline to another persisted conversation thread."""

        if self._ai_active or any(not task.done() for task in self._turn_tasks):
            raise RuntimeError("cannot switch conversation while a turn is active")
        if self._store.get_conversation_thread(thread_id) is None:
            raise ValueError("unknown conversation thread")
        self._session_id = thread_id

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel_watchdog()
        for task in list(self._turn_tasks):
            task.cancel()
        for task in list(self._turn_tasks):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._stt.close()
        await self._tts.close()
        await self._playback.close()

    async def wait_for_turns(self, *, timeout_s: float = 5.0) -> bool:
        """Await outstanding turn work; used by shutdown and by the benchmark."""

        pending = [task for task in self._turn_tasks if not task.done()]
        if not pending:
            return True
        done, _ = await asyncio.wait(pending, timeout=timeout_s)
        return len(done) == len(pending)

    @property
    def _ai_active(self) -> bool:
        return self.state in ("thinking", "speaking")

    def _idle_state(self) -> AppState:
        if self._budget_locked:
            return "budget_locked"
        return self._devices.state

    def _set_state(self, state: AppState, generation_id: int) -> None:
        if not self._coordinator.is_current(generation_id):
            return
        self._write_state(state)

    def _write_state(self, state: AppState) -> None:
        previous = self._state
        self._state = state
        self._state_generation = self._coordinator.generation_id
        if previous != state:
            self._emit(event="state_changed", state=state, previous_state=previous)

    async def _handle_gate_event(self, event: TurnGateEvent) -> None:
        if isinstance(event, TurnStarted):
            if self._ai_active:
                await self._coordinator.cancel("barge_in")
            return
        await self._submit_utterance(event)

    async def _submit_utterance(self, event: UtteranceCaptured) -> None:
        if not self._coordinator.is_current(event.generation_id):
            self._emit(event="utterance_dropped", generation_id=event.generation_id)
            return
        request = TranscriptionRequest(
            request_id=uuid4().hex,
            generation_id=event.generation_id,
            audio=event.audio,
        )
        self._record_speech_end(event.generation_id, event)
        if not self._stt.submit(request):
            self._emit(event="stt_rejected", generation_id=event.generation_id)
            return
        self._set_state("thinking", event.generation_id)
        self._arm_watchdog(event.generation_id)

    def _record_speech_end(self, generation_id: int, event: UtteranceCaptured) -> None:
        # Prefer the capture clock. Subtracting the trailing silence from "now"
        # silently adds however far the pipeline trails the microphone to the
        # start of the end-to-end measurement, which can only make the reported
        # latency shorter than it really was.
        captured_at = (
            None
            if self._sample_clock is None
            else self._sample_clock.wall_time_of_sample(event.last_voiced_sample)
        )
        if captured_at is None:
            trailing_seconds = event.trailing_silence_frames / event.audio.sample_rate
            captured_at = self._monotonic() - trailing_seconds
        self._speech_end_at[generation_id] = captured_at
        if len(self._speech_end_at) > 128:
            for stale in sorted(self._speech_end_at)[:-128]:
                self._speech_end_at.pop(stale, None)

    def _arm_watchdog(self, generation_id: int) -> None:
        self._cancel_watchdog()
        self._watchdog = asyncio.create_task(
            self._watch_stt(generation_id), name="lune-stt-watchdog"
        )

    def _cancel_watchdog(self) -> None:
        watchdog = self._watchdog
        self._watchdog = None
        if watchdog is not None and not watchdog.done():
            watchdog.cancel()

    async def _watch_stt(self, generation_id: int) -> None:
        try:
            await asyncio.sleep(self._stt_timeout_s)
        except asyncio.CancelledError:
            return
        if not self._coordinator.is_current(generation_id):
            return
        await self._coordinator.cancel("stt_timeout")
        # Cancelling moved the fence, so the generation-guarded setter would
        # refuse this transition; the timeout still has to be visible.
        self._write_state("error")
        self._emit(event="stt_timeout", generation_id=generation_id)

    async def _cancel_for_devices(self, reason: str) -> None:
        del reason
        await self._coordinator.cancel("device_changed")

    def _schedule_turn(self, transcript: FinalTranscript, *, speak_text: bool) -> None:
        task = asyncio.create_task(
            self._run_turn(transcript, speak_text=speak_text), name="lune-turn"
        )
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    async def _run_turn(self, transcript: FinalTranscript, *, speak_text: bool) -> None:
        generation_id = transcript.generation_id
        text = transcript.text.strip()
        if not text or not self._coordinator.is_current(generation_id):
            self._set_state(self._idle_state(), generation_id)
            return
        turn_id = self._store.begin_turn(self._session_id, generation_id)
        self._store.accept_user_transcript(turn_id, text)
        turn = _ActiveTurn(generation_id, turn_id, ToolCallValidator(), speak_text)
        turn.validator.begin_turn(0)
        self._active_turn = turn
        self._set_state("thinking", generation_id)
        try:
            result = await self._generate(turn, text)
        except asyncio.CancelledError:
            raise
        except Exception:
            # An unexpected provider fault still has to finish the turn. Without
            # this the task dies with its exception unretrieved, the turn row
            # stays pending, no report is written, and the session sits in
            # `thinking` for an answer that will never arrive, so the microphone
            # keeps demanding the 300 ms barge-in threshold.
            self._emit(event="generation_failed", generation_id=generation_id)
            result = GenerationResult(
                status="error",
                models_attempted=(),
                sentences_emitted=0,
                error_code="provider_error",
            )
        finally:
            if self._active_turn is turn:
                self._active_turn = None
        await self._finish_turn(turn, result)

    async def _generate(self, turn: _ActiveTurn, user_text: str) -> GenerationResult:
        context = self._enricher.enrich(self._session_id, user_text=user_text)
        return await self._generator.generate(
            generation_id=turn.generation_id,
            context=context,
            at=self._now(),
            max_input_tokens=self._estimate_input_tokens(context),
            max_output_tokens=self._max_output_tokens,
        )

    def _estimate_input_tokens(self, context: PromptContext) -> int:
        """Reserve against a worst-case character count; over-reserving is safe."""

        characters = sum(len(message.content) for message in context.recent_messages)
        characters += len(context.summary or "")
        characters += sum(len(memory) for memory in context.relevant_memories)
        estimate = int(characters * CHARACTERS_PER_TOKEN * INPUT_TOKEN_MARGIN) + MIN_INPUT_TOKENS
        return min(self._max_input_tokens, max(MIN_INPUT_TOKENS, estimate))

    async def _finish_turn(self, turn: _ActiveTurn, result: GenerationResult) -> None:
        outcome = self._outcome(turn, result)
        if outcome == "completed":
            self._commit_turn(turn)
        else:
            self._discard_turn(turn)
        if result.status == "budget_locked":
            self._budget_locked = True
        self._reports.append(
            TurnReport(
                generation_id=turn.generation_id,
                outcome=outcome,
                models_attempted=result.models_attempted,
                sentences_played=turn.played_sentences,
                degraded_tts=turn.degraded,
                rejected_tool_calls=turn.rejected_tool_calls,
            )
        )
        if outcome == "error":
            self._set_state("error", turn.generation_id)
        else:
            self._set_state(self._idle_state(), turn.generation_id)
        if outcome == "completed":
            # Both of these run after the state is back to idle, so neither can
            # hold the microphone in `thinking` for work the user never asked
            # for. They are still awaited inside the turn task, so shutdown
            # cancels them the same way it cancels a generation.
            await self.name_thread_if_due(turn.generation_id)
            await self.summarize_if_due()

    def _outcome(self, turn: _ActiveTurn, result: GenerationResult) -> TurnOutcome:
        if result.status == "budget_locked":
            return "budget_locked"
        if not self._coordinator.is_current(turn.generation_id) or result.status == "cancelled":
            return "cancelled"
        if turn.failed or not turn.spoke or result.status == "error":
            return "error"
        return "completed"

    def _commit_turn(self, turn: _ActiveTurn) -> None:
        # Proposals are validated against a pending turn, so they must be
        # committed before the turn itself is marked complete.
        self._proposals.commit_generation(
            turn.generation_id,
            is_generation_current=self._coordinator.is_current,
        )
        try:
            self._store.complete_turn(turn.turn_id)
        except ValueError:
            self._discard_turn(turn)

    def _discard_turn(self, turn: _ActiveTurn) -> None:
        self._proposals.cancel_generation(turn.generation_id)
        try:
            self._store.cancel_turn(turn.turn_id)
        except ValueError:
            return

    async def name_thread_if_due(self, generation_id: int) -> str | None:
        """Generate this thread's one automatic title, off the answering path.

        The fence passed in is the generation that earned the turn, not whatever
        is current when the model finally answers: a barge-in that lands while
        the title is being written invalidates it, exactly as it invalidates the
        turn's own transcript.  Failure and cancellation are indistinguishable
        here on purpose, because both mean "keep the default title".
        """

        if self._titler is None:
            return None
        title = await self._titler.maybe_title(
            self._session_id,
            generation_id=generation_id,
            is_generation_current=self._coordinator.is_current,
        )
        if title is not None:
            self._emit(event="thread_titled", generation_id=generation_id)
        return title

    async def summarize_if_due(self) -> bool:
        """Run the 13th-turn rolling summary outside the latency-critical path."""

        if self._summarizer is None:
            return False
        generation_id = self._coordinator.generation_id
        coverage = await self._summarizer.maybe_summarize(
            self._session_id,
            generation_id=generation_id,
            is_generation_current=self._coordinator.is_current,
        )
        return coverage is not None

    async def _on_llm_frame(self, frame: ProviderStreamFrame) -> None:
        turn = self._active_turn
        if turn is None or frame.generation_id != turn.generation_id:
            return
        if not self._coordinator.is_current(frame.generation_id):
            return
        if isinstance(frame, GenerationFunctionCallFrame):
            self._handle_tool_call(turn, frame)
            return
        if isinstance(frame, GenerationLLMTextFrame):
            if turn.speak_text:
                await self._speak(turn, frame.text)
            else:
                self._deliver_text(turn, frame.text)

    def _deliver_text(self, turn: _ActiveTurn, text: str) -> None:
        """Commit a complete text sentence without pretending it was spoken."""

        sentence = text.strip()
        if not sentence or not self._coordinator.is_current(turn.generation_id):
            return
        try:
            self._store.append_assistant_text_delivery(turn.turn_id, sentence)
        except ValueError:
            turn.failed = True
            return
        turn.spoke = True
        turn.played_sentences += 1

    def _handle_tool_call(self, turn: _ActiveTurn, frame: GenerationFunctionCallFrame) -> None:
        arguments = frame.arguments
        payload_json = arguments if isinstance(arguments, str) else json.dumps(arguments)
        if not isinstance(payload_json, str):
            turn.rejected_tool_calls += 1
            return
        outcome = turn.validator.validate(frame.function_name, payload_json)
        if not outcome.accepted:
            turn.rejected_tool_calls += 1
            self._emit(event="tool_call_rejected", generation_id=turn.generation_id)
            return
        payload = json.loads(payload_json)
        try:
            self._record_proposal(turn, frame.function_name, payload)
        except ValueError:
            turn.rejected_tool_calls += 1

    def _record_proposal(
        self,
        turn: _ActiveTurn,
        function_name: str,
        payload: dict[str, object],
    ) -> None:
        if function_name == PROPOSE_MEMORY:
            self._proposals.propose_memory(
                MemoryProposal(
                    proposal_id=uuid4().hex,
                    generation_id=turn.generation_id,
                    session_id=self._session_id,
                    turn_id=turn.turn_id,
                    category=str(payload["category"]),
                    importance=float(payload["importance"]),  # type: ignore[arg-type]
                    content=str(payload["content"]),
                )
            )
            return
        if function_name != PROPOSE_AFFINITY:
            raise ValueError("unsupported tool")
        self._proposals.propose_affinity(
            AffinityProposal(
                proposal_id=uuid4().hex,
                generation_id=turn.generation_id,
                session_id=self._session_id,
                turn_id=turn.turn_id,
                delta=int(payload["delta"]),  # type: ignore[call-overload]
                reason=str(payload["reason"]),
            )
        )

    async def _speak(self, turn: _ActiveTurn, text: str) -> None:
        sentence = text.strip()
        if not sentence:
            return
        generation_id = turn.generation_id
        self._set_state("speaking", generation_id)
        request = TTSRequest(
            request_id=uuid4().hex,
            generation_id=generation_id,
            text=sentence,
        )
        try:
            async for chunk in self._tts.synthesize(request):
                if not self._coordinator.is_current(generation_id):
                    return
                if not await self._playback.submit(chunk):
                    await self._handle_playback_rejection(generation_id)
                    return
        except TTSBackendError as error:
            if error.code != "cancelled":
                turn.failed = True
                self._emit(event="tts_failed", generation_id=generation_id, backend="router")
            return
        if self._tts.was_degraded(generation_id):
            turn.degraded = True
            self._degraded_tts = True
        if not await self._playback.drain(generation_id):
            return
        if not self._coordinator.is_current(generation_id):
            return
        try:
            self._store.append_assistant_playback(turn.turn_id, sentence)
        except ValueError:
            turn.failed = True
            return
        turn.spoke = True
        turn.played_sentences += 1

    async def _handle_playback_rejection(self, generation_id: int) -> None:
        health = self._playback.health()
        if not health.overflowed:
            return
        self._emit(
            event="playback_overflow",
            generation_id=generation_id,
            queue_depth=health.queue_depth,
            count=health.dropped_chunks,
        )
        await self._coordinator.cancel("output_overflow")

    def _emit(self, **fields: object) -> None:
        if self._diagnostics is not None:
            self._diagnostics.emit(**fields)
