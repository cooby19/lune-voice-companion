"""Sample-count turn policy kept separate from the quantized Silero analyzer."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from lune.audio.types import milliseconds_to_samples


class TurnEventKind(StrEnum):
    TURN_STARTED = "turn_started"
    BARGE_IN_CONFIRMED = "barge_in_confirmed"
    TURN_ENDED = "turn_ended"


@dataclass(frozen=True, slots=True)
class TurnEvent:
    kind: TurnEventKind
    at_sample: int
    voice_onset_sample: int | None = None


@dataclass(frozen=True, slots=True)
class TurnPolicyConfig:
    sample_rate: int = 16_000
    idle_start_ms: int = 100
    barge_in_ms: int = 300
    end_silence_ms: int = 350

    @property
    def idle_start_samples(self) -> int:
        return milliseconds_to_samples(self.idle_start_ms, self.sample_rate)

    @property
    def barge_in_samples(self) -> int:
        return milliseconds_to_samples(self.barge_in_ms, self.sample_rate)

    @property
    def end_silence_samples(self) -> int:
        return milliseconds_to_samples(self.end_silence_ms, self.sample_rate)


class TurnPolicy:
    """Consume exact voiced spans; Silero supplies only the voiced boolean."""

    def __init__(self, config: TurnPolicyConfig | None = None) -> None:
        self.config = config or TurnPolicyConfig()
        self._next_sample: int | None = None
        self._voice_onset: int | None = None
        self._silence_onset: int | None = None
        self._turn_active = False

    @property
    def turn_active(self) -> bool:
        return self._turn_active

    def reset(self) -> None:
        self._next_sample = None
        self._voice_onset = None
        self._silence_onset = None
        self._turn_active = False

    def feed(
        self, *, start_sample: int, end_sample: int, voiced: bool, ai_playing: bool
    ) -> tuple[TurnEvent, ...]:
        if start_sample < 0 or end_sample <= start_sample:
            raise ValueError("invalid VAD observation range")
        if self._next_sample is not None and start_sample != self._next_sample:
            raise ValueError("VAD observations must be contiguous")
        self._next_sample = end_sample

        if not self._turn_active:
            return self._feed_waiting(
                start_sample=start_sample,
                end_sample=end_sample,
                voiced=voiced,
                ai_playing=ai_playing,
            )
        return self._feed_active(start_sample=start_sample, end_sample=end_sample, voiced=voiced)

    def _feed_waiting(
        self, *, start_sample: int, end_sample: int, voiced: bool, ai_playing: bool
    ) -> tuple[TurnEvent, ...]:
        if not voiced:
            self._voice_onset = None
            return ()
        if self._voice_onset is None:
            self._voice_onset = start_sample
        threshold = self.config.barge_in_samples if ai_playing else self.config.idle_start_samples
        confirmation = self._voice_onset + threshold
        if end_sample < confirmation:
            return ()
        kind = TurnEventKind.BARGE_IN_CONFIRMED if ai_playing else TurnEventKind.TURN_STARTED
        self._turn_active = True
        self._silence_onset = None
        return (
            TurnEvent(
                kind=kind,
                at_sample=confirmation,
                voice_onset_sample=self._voice_onset,
            ),
        )

    def _feed_active(
        self, *, start_sample: int, end_sample: int, voiced: bool
    ) -> tuple[TurnEvent, ...]:
        if voiced:
            self._silence_onset = None
            return ()
        if self._silence_onset is None:
            self._silence_onset = start_sample
        confirmation = self._silence_onset + self.config.end_silence_samples
        if end_sample < confirmation:
            return ()
        self._turn_active = False
        self._voice_onset = None
        self._silence_onset = None
        return (TurnEvent(kind=TurnEventKind.TURN_ENDED, at_sample=confirmation),)
