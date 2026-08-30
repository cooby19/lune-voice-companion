"""Deterministic composition of the full M6 path for public tests."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import numpy.typing as npt

from lune.audio.types import AudioSpan
from lune.llm.budget import BudgetLedger
from lune.llm.contracts import ModelName, ProviderStreamFrame
from lune.llm.streaming import ScriptedAttemptProvider, StreamFrameFactory
from lune.memory.embedding import E5_MODEL_ID, E5_MODEL_REVISION, E5MemoryRetriever
from lune.memory.store import EMBEDDING_DIMENSIONS, MemoryStore
from lune.memory.titles import ThreadTitleBackend, ThreadTitleManager
from lune.pipeline.factory import VoicePipeline, build_voice_pipeline
from lune.stt.contracts import FinalTranscript, STTEvent, STTFailure, TranscriptionRequest
from lune.tts.contracts import PCMChunk, StreamingTTSBackend, TTSBackendError, TTSRequest
from lune.tts.router import TTSRouterService
from tests.pipeline.conftest import NATIVE_WINDOW, FakeDetector, audio_span

type FloatArray = npt.NDArray[np.float32]
type STTEventSink = Callable[[STTEvent], object]

SESSION_ID = "session-under-test"
LEAD_SILENCE = NATIVE_WINDOW * 11
END_SILENCE_FRAMES = 5_600 + NATIVE_WINDOW


class HashEncoder:
    """Stable 384-dimension vectors so retrieval is deterministic without a model."""

    model_id = E5_MODEL_ID
    revision = E5_MODEL_REVISION

    def encode_query(self, query: str) -> FloatArray:
        return self._vector(query)

    def encode_passages(self, passages: Sequence[str]) -> FloatArray:
        return np.stack([self._vector(item) for item in passages])

    def _vector(self, value: str) -> FloatArray:
        digest = hashlib.sha256(value.encode("utf-8")).digest()
        raw = np.frombuffer(digest * 12, dtype=np.uint8)[:EMBEDDING_DIMENSIONS]
        vector = raw.astype(np.float32) + 1.0
        return np.asarray(vector / np.linalg.norm(vector), dtype="<f4")


class FakeSTT:
    def __init__(self, sink: STTEventSink) -> None:
        self._sink = sink
        self.generation_id = 0
        self.requests: list[TranscriptionRequest] = []
        self.accepting = True
        self.closed = False

    def set_generation(self, generation_id: int) -> None:
        self.generation_id = generation_id

    def submit(self, request: TranscriptionRequest) -> bool:
        if not self.accepting or request.generation_id != self.generation_id:
            return False
        self.requests.append(request)
        return True

    async def close(self) -> None:
        self.closed = True

    async def emit_final(self, text: str, *, generation_id: int | None = None) -> None:
        request = self.requests[-1]
        await self._sink(  # type: ignore[misc]
            FinalTranscript(
                request_id=request.request_id,
                generation_id=(request.generation_id if generation_id is None else generation_id),
                text=text,
            )
        )

    async def emit_failure(self, code: str) -> None:
        request = self.requests[-1]
        await self._sink(  # type: ignore[misc]
            STTFailure(
                request_id=request.request_id,
                generation_id=request.generation_id,
                code=code,  # type: ignore[arg-type]
            )
        )


class ScriptedTTSBackend:
    """Yield fixed PCM per utterance, optionally pausing before finishing."""

    def __init__(
        self,
        *,
        chunks: int = 2,
        error: TTSBackendError | None = None,
        pause: Callable[[], object] | None = None,
    ) -> None:
        self.requests: list[TTSRequest] = []
        self.cancelled: list[int] = []
        self.closed = False
        self._chunks = chunks
        self._error = error
        self._pause = pause

    async def synthesize(self, request: TTSRequest) -> AsyncIterator[PCMChunk]:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        for index in range(self._chunks):
            if index and self._pause is not None:
                await self._pause()  # type: ignore[misc]
            yield PCMChunk(
                generation_id=request.generation_id,
                sample_rate=24_000,
                channels=1,
                data=np.full(8, 6_000, dtype="<i2").tobytes(),
            )

    async def cancel(self, generation_id: int) -> None:
        self.cancelled.append(generation_id)

    async def close(self) -> None:
        self.closed = True


class RecordingProviderFence:
    def __init__(self) -> None:
        self.interrupted: list[int] = []

    async def interrupt_and_drain(self, generation_id: int) -> None:
        self.interrupted.append(generation_id)


@dataclass
class Harness:
    pipeline: VoicePipeline
    store: MemoryStore
    stt: FakeSTT
    backend: ScriptedTTSBackend
    terra: ScriptedAttemptProvider
    luna: ScriptedAttemptProvider
    fence: RecordingProviderFence
    ledger: BudgetLedger
    device: object
    sample_cursor: int = field(default=0)

    async def feed(self, frames: int, *, voiced: bool) -> None:
        offset = 0
        while offset < frames:
            size = min(NATIVE_WINDOW, frames - offset)
            span = audio_span(
                self.sample_cursor,
                size,
                voiced=voiced,
                generation_id=self.pipeline.session.generation_id,
            )
            await self.pipeline.session.handle_audio(span)
            self.sample_cursor += size
            offset += size

    async def speak_utterance(self, *, voiced_frames: int = NATIVE_WINDOW * 8) -> None:
        """Feed lead-in, speech and the end-of-turn silence that closes the turn."""

        if self.sample_cursor == 0:
            await self.feed(LEAD_SILENCE, voiced=False)
        await self.feed(voiced_frames, voiced=True)
        await self.feed(END_SILENCE_FRAMES, voiced=False)

    async def start_barge_in(self, *, voiced_frames: int = NATIVE_WINDOW * 12) -> None:
        """Feed enough speech to confirm an interruption without ending the turn."""

        await self.feed(voiced_frames, voiced=True)


def build_harness(
    tmp_path: Path,
    *,
    terra_scripts: Sequence[Sequence[StreamFrameFactory]] = (),
    luna_scripts: Sequence[Sequence[StreamFrameFactory]] = (),
    terra_drains: Sequence[Sequence[StreamFrameFactory]] = (),
    backend: ScriptedTTSBackend | None = None,
    ledger: BudgetLedger | None = None,
    playback_capacity: int = 32,
    stt_timeout_s: float = 10.0,
    sample_clock: object | None = None,
    title_backend: ThreadTitleBackend | None = None,
) -> Harness:
    from tests.pipeline.conftest import RecordingOutputDevice

    store = MemoryStore(tmp_path / "private" / "lune.sqlite3")
    store.start_session(SESSION_ID)
    retriever = E5MemoryRetriever(store, HashEncoder())
    stt_holder: list[FakeSTT] = []

    def stt_factory(sink: STTEventSink) -> FakeSTT:
        stt = FakeSTT(sink)
        stt_holder.append(stt)
        return stt

    terra = ScriptedAttemptProvider("gpt-5.6-terra", scripts=terra_scripts, drains=terra_drains)
    luna = ScriptedAttemptProvider("gpt-5.6-luna", scripts=luna_scripts)
    providers: dict[ModelName, ScriptedAttemptProvider] = {
        "gpt-5.6-terra": terra,
        "gpt-5.6-luna": luna,
    }
    tts_backend = backend or ScriptedTTSBackend()
    budget = ledger or BudgetLedger()
    router = TTSRouterService(avspeech=_as_backend(tts_backend))
    device = RecordingOutputDevice()
    fence = RecordingProviderFence()
    pipeline = build_voice_pipeline(
        session_id=SESSION_ID,
        store=store,
        retriever=retriever,
        detector=FakeDetector(),
        stt_factory=stt_factory,  # type: ignore[arg-type]
        providers=providers,
        ledger=budget,
        tts=router,
        output_device=device,
        provider_fences=(fence,),
        titler=None if title_backend is None else ThreadTitleManager(store, title_backend),
        playback_capacity=playback_capacity,
        stt_timeout_s=stt_timeout_s,
        sample_clock=sample_clock,  # type: ignore[arg-type]
    )
    return Harness(
        pipeline=pipeline,
        store=store,
        stt=stt_holder[0],
        backend=tts_backend,
        terra=terra,
        luna=luna,
        fence=fence,
        ledger=budget,
        device=device,
    )


def _as_backend(backend: ScriptedTTSBackend) -> StreamingTTSBackend:
    return backend


def frame_span(start: int, frames: int, *, voiced: bool, generation_id: int) -> AudioSpan:
    return audio_span(start, frames, voiced=voiced, generation_id=generation_id)


def released(frames: Sequence[ProviderStreamFrame]) -> list[str]:
    return [getattr(frame, "text", "") for frame in frames]
