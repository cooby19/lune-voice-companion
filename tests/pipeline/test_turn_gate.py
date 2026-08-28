from __future__ import annotations

import numpy as np
import pytest

from lune.audio.preroll import PreRollBuffer
from lune.audio.vad import TurnPolicy, TurnPolicyConfig
from lune.pipeline.contracts import TurnStarted, UtteranceCaptured
from lune.pipeline.turn_gate import VoiceTurnGate
from tests.pipeline.conftest import NATIVE_WINDOW, FakeDetector, audio_span, drive

IDLE_START_SAMPLES = 1_600
BARGE_IN_SAMPLES = 4_800
END_SILENCE_SAMPLES = 5_600
PRE_ROLL_SAMPLES = 5_600
LEAD_SILENCE = NATIVE_WINDOW * 11
"""Window-aligned lead-in so the analyzer's 512-sample grid cannot blur onsets."""


def build_gate(*, generation_id: int = 0) -> VoiceTurnGate:
    return VoiceTurnGate(
        detector=FakeDetector(),
        policy=TurnPolicy(TurnPolicyConfig()),
        pre_roll=PreRollBuffer(),
        generation_id=generation_id,
    )


def feed_stream(
    gate: VoiceTurnGate,
    *,
    start: int,
    frames: int,
    voiced: bool,
    generation_id: int = 0,
    ai_active: bool = False,
) -> list[object]:
    events: list[object] = []
    offset = 0
    while offset < frames:
        size = min(NATIVE_WINDOW, frames - offset)
        span = audio_span(start + offset, size, voiced=voiced, generation_id=generation_id)
        events.extend(drive(gate, span, ai_active=ai_active))
        offset += size
    return events


def only_started(events: list[object]) -> list[TurnStarted]:
    return [event for event in events if isinstance(event, TurnStarted)]


def only_captured(events: list[object]) -> list[UtteranceCaptured]:
    return [event for event in events if isinstance(event, UtteranceCaptured)]


def test_idle_turn_starts_after_one_hundred_milliseconds_of_speech() -> None:
    gate = build_gate()
    assert feed_stream(gate, start=0, frames=LEAD_SILENCE, voiced=False) == []

    events = feed_stream(gate, start=LEAD_SILENCE, frames=NATIVE_WINDOW * 4, voiced=True)
    started = only_started(events)
    assert len(started) == 1
    assert started[0].barge_in is False
    assert started[0].voice_onset_sample == LEAD_SILENCE
    assert started[0].at_sample == LEAD_SILENCE + IDLE_START_SAMPLES


def test_utterance_carries_the_full_pre_roll_and_stops_at_the_last_voiced_sample() -> None:
    gate = build_gate()
    feed_stream(gate, start=0, frames=LEAD_SILENCE, voiced=False)
    speech_frames = NATIVE_WINDOW * 8
    feed_stream(gate, start=LEAD_SILENCE, frames=speech_frames, voiced=True)
    silence_start = LEAD_SILENCE + speech_frames
    events = feed_stream(
        gate,
        start=silence_start,
        frames=END_SILENCE_SAMPLES + NATIVE_WINDOW,
        voiced=False,
    )

    captured = only_captured(events)
    assert len(captured) == 1
    utterance = captured[0]
    assert utterance.pre_roll_truncated is False
    assert utterance.audio.start_sample == LEAD_SILENCE - PRE_ROLL_SAMPLES
    assert utterance.audio.end_sample == silence_start + END_SILENCE_SAMPLES
    assert utterance.last_voiced_sample == silence_start
    assert utterance.audio.frame_count * 2 == len(utterance.audio.pcm)
    samples = np.frombuffer(utterance.audio.pcm, dtype="<i2")
    assert int(np.count_nonzero(samples)) == speech_frames


def test_speech_while_playing_needs_three_hundred_milliseconds_to_barge_in() -> None:
    gate = build_gate()
    feed_stream(gate, start=0, frames=LEAD_SILENCE, voiced=False, ai_active=True)

    short = feed_stream(
        gate,
        start=LEAD_SILENCE,
        frames=NATIVE_WINDOW * 8,
        voiced=True,
        ai_active=True,
    )
    assert only_started(short) == []

    events = feed_stream(
        gate,
        start=LEAD_SILENCE + NATIVE_WINDOW * 8,
        frames=NATIVE_WINDOW * 4,
        voiced=True,
        ai_active=True,
    )
    started = only_started(events)
    assert len(started) == 1
    assert started[0].barge_in is True
    assert started[0].at_sample == LEAD_SILENCE + BARGE_IN_SAMPLES


def test_barge_in_carry_over_keeps_the_interrupting_speech_and_re_stamps_it() -> None:
    gate = build_gate()
    feed_stream(gate, start=0, frames=LEAD_SILENCE, voiced=False, ai_active=True)
    confirmed = feed_stream(
        gate,
        start=LEAD_SILENCE,
        frames=NATIVE_WINDOW * 12,
        voiced=True,
        ai_active=True,
    )
    assert only_started(confirmed)[0].barge_in is True
    assert gate.turn_active is True

    gate.carry_over_generation(1)
    assert gate.generation_id == 1

    # PCM still queued from the interrupted generation belongs to this utterance.
    resumed = LEAD_SILENCE + NATIVE_WINDOW * 12
    feed_stream(gate, start=resumed, frames=NATIVE_WINDOW * 4, voiced=True, generation_id=0)
    silence_start = resumed + NATIVE_WINDOW * 4
    events = feed_stream(
        gate,
        start=silence_start,
        frames=END_SILENCE_SAMPLES + NATIVE_WINDOW,
        voiced=False,
        generation_id=1,
    )

    captured = only_captured(events)
    assert len(captured) == 1
    assert captured[0].generation_id == 1
    assert captured[0].audio.generation_id == 1
    assert captured[0].audio.start_sample == LEAD_SILENCE - PRE_ROLL_SAMPLES
    assert captured[0].last_voiced_sample == silence_start
    samples = np.frombuffer(captured[0].audio.pcm, dtype="<i2")
    assert int(np.count_nonzero(samples)) == silence_start - LEAD_SILENCE


def test_carried_generation_is_dropped_once_the_new_stream_arrives() -> None:
    gate = build_gate()
    feed_stream(gate, start=0, frames=NATIVE_WINDOW, voiced=False)
    gate.carry_over_generation(1)

    fresh = audio_span(NATIVE_WINDOW, NATIVE_WINDOW, voiced=False, generation_id=1)
    assert drive(gate, fresh) == []
    stale = audio_span(NATIVE_WINDOW * 2, NATIVE_WINDOW, voiced=True, generation_id=0)
    assert drive(gate, stale) == []
    assert gate.pending_windows == 0


def test_reset_discards_every_buffered_sample() -> None:
    gate = build_gate()
    feed_stream(gate, start=0, frames=LEAD_SILENCE, voiced=False)
    feed_stream(gate, start=LEAD_SILENCE, frames=NATIVE_WINDOW * 8, voiced=True)
    assert gate.turn_active is True

    gate.reset_generation(2)
    assert gate.turn_active is False
    assert gate.pending_windows == 0
    assert drive(gate, audio_span(0, NATIVE_WINDOW, voiced=True, generation_id=0)) == []

    events = feed_stream(gate, start=0, frames=NATIVE_WINDOW * 4, voiced=True, generation_id=2)
    started = only_started(events)
    assert len(started) == 1
    assert started[0].generation_id == 2


def test_generations_never_move_backwards() -> None:
    gate = build_gate(generation_id=3)
    with pytest.raises(ValueError):
        gate.reset_generation(2)
    with pytest.raises(ValueError):
        gate.carry_over_generation(1)


def test_a_long_utterance_is_bounded_instead_of_growing_without_limit() -> None:
    gate = VoiceTurnGate(
        detector=FakeDetector(),
        policy=TurnPolicy(TurnPolicyConfig()),
        pre_roll=PreRollBuffer(),
        max_utterance_ms=1_000,
    )
    events = feed_stream(gate, start=0, frames=16_000 * 2, voiced=True)
    captured = only_captured(events)
    assert captured
    assert captured[0].max_length_reached is True
    assert captured[0].audio.frame_count == 16_000
    assert captured[0].last_voiced_sample == captured[0].audio.end_sample


def test_dropped_input_restarts_the_timeline_instead_of_corrupting_it() -> None:
    gate = build_gate()
    feed_stream(gate, start=0, frames=LEAD_SILENCE, voiced=False)
    feed_stream(gate, start=LEAD_SILENCE, frames=NATIVE_WINDOW * 8, voiced=True)
    assert gate.turn_active is True

    gap = LEAD_SILENCE + NATIVE_WINDOW * 8 + 10_000
    drive(gate, audio_span(gap, NATIVE_WINDOW, voiced=True))
    assert gate.discontinuities == 1
    assert gate.turn_active is False
