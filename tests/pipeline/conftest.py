from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from lune.audio.types import AudioSpan
from lune.pipeline.contracts import TurnGateEvent
from lune.pipeline.turn_gate import VoiceTurnGate
from lune.tts.contracts import PCMChunk

NATIVE_WINDOW = 512
VOICED_AMPLITUDE = 8_000


class FakeDetector:
    """Classify one native window by amplitude so fixtures stay deterministic."""

    def __init__(self, *, frames_required: int = NATIVE_WINDOW, threshold: int = 1_000) -> None:
        self._frames_required = frames_required
        self._threshold = threshold

    @property
    def frames_required(self) -> int:
        return self._frames_required

    def is_voiced(self, span: AudioSpan) -> bool:
        samples = np.frombuffer(span.pcm, dtype="<i2")
        return bool(np.abs(samples.astype(np.int32)).max(initial=0) > self._threshold)


def audio_span(
    start_sample: int,
    frames: int,
    *,
    voiced: bool,
    generation_id: int = 0,
) -> AudioSpan:
    amplitude = VOICED_AMPLITUDE if voiced else 0
    pcm = np.full(frames, amplitude, dtype="<i2").tobytes()
    return AudioSpan(
        pcm=pcm,
        start_sample=start_sample,
        end_sample=start_sample + frames,
        generation_id=generation_id,
    )


def drive(
    gate: VoiceTurnGate,
    span: AudioSpan,
    *,
    ai_active: bool = False,
) -> list[TurnGateEvent]:
    """Feed one span the way the session does: pump until no window is left."""

    events = list(gate.feed(span, ai_active=ai_active))
    while gate.pending_windows:
        drained = gate.pump(ai_active=ai_active)
        if not drained:
            break
        events.extend(drained)
    return events


def pcm_chunk(
    generation_id: int,
    *,
    amplitude: int = VOICED_AMPLITUDE,
    frames: int = 8,
) -> PCMChunk:
    data = np.full(frames, amplitude, dtype="<i2").tobytes()
    return PCMChunk(generation_id=generation_id, sample_rate=24_000, channels=1, data=data)


class RecordingOutputDevice:
    def __init__(self) -> None:
        self.written: list[PCMChunk] = []
        self.flushes = 0
        self.closed = False

    async def write(self, chunk: PCMChunk) -> None:
        self.written.append(chunk)

    async def flush(self) -> None:
        self.flushes += 1

    async def close(self) -> None:
        self.closed = True

    def generations(self) -> Sequence[int]:
        return [chunk.generation_id for chunk in self.written]
