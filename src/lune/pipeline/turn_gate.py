"""Generation-fenced turn detection that assembles one complete utterance."""

from __future__ import annotations

from typing import Protocol

from lune.audio.preroll import PreRollBuffer
from lune.audio.types import BYTES_PER_SAMPLE, AudioSpan, milliseconds_to_samples
from lune.audio.vad import TurnEvent, TurnEventKind, TurnPolicy
from lune.pipeline.contracts import TurnGateEvent, TurnStarted, UtteranceCaptured


class VoicedDetector(Protocol):
    @property
    def frames_required(self) -> int: ...

    def is_voiced(self, span: AudioSpan) -> bool: ...


class VoiceTurnGate:
    """Drive ``TurnPolicy`` and ``PreRollBuffer`` behind one explicit fence.

    M1 deliberately left both primitives generation-agnostic. Here they are given
    an owner: PCM is only accepted for the current generation, and every turn
    event is resolved into its capture inside the same call, so a delayed event
    can never reach forward into audio that now belongs to a newer generation.

    A barge-in is the one case where audio must survive the fence, because the
    speech that interrupted Lune is the next utterance. ``carry_over_generation``
    keeps the in-flight utterance and accepts the interrupted generation's
    still-queued PCM exactly until the re-stamped stream catches up.
    """

    def __init__(
        self,
        *,
        detector: VoicedDetector,
        policy: TurnPolicy | None = None,
        pre_roll: PreRollBuffer | None = None,
        generation_id: int = 0,
        sample_rate: int = 16_000,
        channels: int = 1,
        max_utterance_ms: int = 30_000,
    ) -> None:
        if generation_id < 0:
            raise ValueError("generation ID cannot be negative")
        if channels != 1:
            raise ValueError("turn detection requires mono audio")
        self._detector = detector
        self._policy = policy or TurnPolicy()
        self._pre_roll = pre_roll or PreRollBuffer(sample_rate=sample_rate, channels=channels)
        self.sample_rate = sample_rate
        self.channels = channels
        self._bytes_per_frame = channels * BYTES_PER_SAMPLE
        self._max_utterance_samples = milliseconds_to_samples(max_utterance_ms, sample_rate)
        if self._max_utterance_samples <= self._pre_roll.pre_roll_samples:
            raise ValueError("utterance bound must exceed the pre-roll window")
        self._generation_id = generation_id
        self._carried_generation: int | None = None
        self._window = bytearray()
        self._window_start: int | None = None
        self._utterance = bytearray()
        self._utterance_start: int | None = None
        self._utterance_end = 0
        self._pre_roll_truncated = False
        self._discontinuities = 0

    @property
    def generation_id(self) -> int:
        return self._generation_id

    @property
    def turn_active(self) -> bool:
        return self._policy.turn_active

    @property
    def discontinuities(self) -> int:
        """How often dropped input forced the gate to restart its sample timeline."""

        return self._discontinuities

    def reset_generation(self, generation_id: int) -> None:
        """Discard every buffered sample; nothing crosses this fence."""

        self._check_forward(generation_id)
        self._generation_id = generation_id
        self._carried_generation = None
        self._policy.reset()
        self._pre_roll.clear()
        self._window = bytearray()
        self._window_start = None
        self._discard_utterance()

    def carry_over_generation(self, generation_id: int) -> None:
        """Keep the interrupting speech and re-stamp it into the new generation."""

        self._check_forward(generation_id)
        if generation_id != self._generation_id:
            self._carried_generation = self._generation_id
        self._generation_id = generation_id
        # The ring's only job is recovering speech onset, and that already lives
        # in the in-flight utterance, so it restarts under the new generation.
        self._pre_roll.clear()

    @property
    def pending_windows(self) -> int:
        """Complete analysis windows buffered but not yet classified."""

        window_bytes = self._detector.frames_required * self._bytes_per_frame
        return len(self._window) // window_bytes

    def feed(self, span: AudioSpan, *, ai_active: bool) -> tuple[TurnGateEvent, ...]:
        accepted = self._accept(span)
        if accepted is None or accepted.frame_count == 0:
            return ()
        buffered = len(self._window) // self._bytes_per_frame
        if self._window_start is None:
            self._window_start = accepted.start_sample
        elif accepted.start_sample != self._window_start + buffered:
            self._restart_timeline(accepted.start_sample)
        self._window.extend(accepted.pcm)
        return self.pump(ai_active=ai_active)

    def pump(self, *, ai_active: bool) -> tuple[TurnGateEvent, ...]:
        """Classify buffered windows, stopping as soon as one produces an event.

        Returning at the first event lets the caller cancel before the next
        window is classified, so a barge-in never captures audio that the
        cancellation is about to move into a newer generation.
        """

        return self._drain_windows(ai_active=ai_active)

    def _accept(self, span: AudioSpan) -> AudioSpan | None:
        if span.sample_rate != self.sample_rate or span.channels != self.channels:
            raise ValueError("PCM format changed without rebuilding the turn gate")
        if span.generation_id == self._generation_id:
            self._carried_generation = None
            return span
        if span.generation_id == self._carried_generation:
            return _restamp(span, self._generation_id)
        return None

    def _restart_timeline(self, start_sample: int) -> None:
        """Input was dropped, so no buffered sample is adjacent to this one."""

        self._discontinuities += 1
        self._policy.reset()
        self._pre_roll.clear()
        self._window = bytearray()
        self._window_start = start_sample
        self._discard_utterance()

    def _drain_windows(self, *, ai_active: bool) -> tuple[TurnGateEvent, ...]:
        frames = self._detector.frames_required
        window_bytes = frames * self._bytes_per_frame
        events: list[TurnGateEvent] = []
        while len(self._window) >= window_bytes:
            assert self._window_start is not None
            start = self._window_start
            end = start + frames
            pcm = bytes(self._window[:window_bytes])
            del self._window[:window_bytes]
            self._window_start = end
            window = AudioSpan(
                pcm=pcm,
                start_sample=start,
                end_sample=end,
                generation_id=self._generation_id,
                sample_rate=self.sample_rate,
                channels=self.channels,
            )
            self._pre_roll.append(window)
            voiced = self._detector.is_voiced(window)
            for event in self._policy.feed(
                start_sample=start,
                end_sample=end,
                voiced=voiced,
                ai_playing=ai_active,
            ):
                if event.kind is TurnEventKind.TURN_ENDED:
                    events.append(self._close_utterance(window, event.at_sample))
                    continue
                events.append(self._open_utterance(event))
            if self._utterance_start is not None:
                self._extend_utterance(window, end)
                bounded = self._enforce_utterance_bound()
                if bounded is not None:
                    events.append(bounded)
            if events:
                break
        return tuple(events)

    def _open_utterance(self, event: TurnEvent) -> TurnStarted:
        onset = event.voice_onset_sample
        if onset is None:
            raise RuntimeError("a turn start must report its voice onset")
        capture = self._pre_roll.capture(
            voice_onset_sample=onset,
            confirmation_sample=event.at_sample,
        )
        self._utterance = bytearray(capture.audio.pcm)
        self._utterance_start = capture.audio.start_sample
        self._utterance_end = event.at_sample
        self._pre_roll_truncated = capture.pre_roll_truncated
        return TurnStarted(
            generation_id=self._generation_id,
            at_sample=event.at_sample,
            voice_onset_sample=onset,
            barge_in=event.kind is TurnEventKind.BARGE_IN_CONFIRMED,
        )

    def _extend_utterance(self, window: AudioSpan, until_sample: int) -> None:
        start = max(self._utterance_end, window.start_sample)
        if start >= until_sample:
            return
        self._utterance.extend(window.slice(start, until_sample).pcm)
        self._utterance_end = until_sample

    def _close_utterance(self, window: AudioSpan, at_sample: int) -> UtteranceCaptured:
        self._extend_utterance(window, at_sample)
        captured = self._build_capture(max_length_reached=False)
        self._discard_utterance()
        return captured

    def _enforce_utterance_bound(self) -> UtteranceCaptured | None:
        assert self._utterance_start is not None
        if self._utterance_end - self._utterance_start < self._max_utterance_samples:
            return None
        captured = self._build_capture(max_length_reached=True)
        self._discard_utterance()
        self._policy.reset()
        return captured

    def _build_capture(self, *, max_length_reached: bool) -> UtteranceCaptured:
        assert self._utterance_start is not None
        end = min(self._utterance_end, self._utterance_start + self._max_utterance_samples)
        length = (end - self._utterance_start) * self._bytes_per_frame
        audio = AudioSpan(
            pcm=bytes(self._utterance[:length]),
            start_sample=self._utterance_start,
            end_sample=end,
            generation_id=self._generation_id,
            sample_rate=self.sample_rate,
            channels=self.channels,
        )
        # A turn ends one end-of-turn silence after the speech itself stopped;
        # a length-bounded capture is cut while the user is still speaking.
        silence = 0 if max_length_reached else self._policy.config.end_silence_samples
        return UtteranceCaptured(
            generation_id=self._generation_id,
            audio=audio,
            last_voiced_sample=max(audio.start_sample, end - silence),
            pre_roll_truncated=self._pre_roll_truncated,
            max_length_reached=max_length_reached,
        )

    def _discard_utterance(self) -> None:
        self._utterance = bytearray()
        self._utterance_start = None
        self._utterance_end = 0
        self._pre_roll_truncated = False

    def _check_forward(self, generation_id: int) -> None:
        if generation_id < self._generation_id:
            raise ValueError("generation ID cannot move backwards")


def _restamp(span: AudioSpan, generation_id: int) -> AudioSpan:
    return AudioSpan(
        pcm=span.pcm,
        start_sample=span.start_sample,
        end_sample=span.end_sample,
        generation_id=generation_id,
        sample_rate=span.sample_rate,
        channels=span.channels,
    )
