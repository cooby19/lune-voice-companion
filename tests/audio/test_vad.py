from __future__ import annotations

import pytest

from lune.audio.vad import TurnEventKind, TurnPolicy

SAMPLES_PER_MS = 16


def feed_ms(
    policy: TurnPolicy,
    start_ms: int,
    end_ms: int,
    *,
    voiced: bool,
    ai_playing: bool,
) -> tuple[TurnEventKind, ...]:
    events = policy.feed(
        start_sample=start_ms * SAMPLES_PER_MS,
        end_sample=end_ms * SAMPLES_PER_MS,
        voiced=voiced,
        ai_playing=ai_playing,
    )
    return tuple(event.kind for event in events)


def test_idle_99ms_does_not_start_but_100ms_does() -> None:
    policy = TurnPolicy()
    assert feed_ms(policy, 0, 99, voiced=True, ai_playing=False) == ()
    assert feed_ms(policy, 99, 100, voiced=True, ai_playing=False) == (TurnEventKind.TURN_STARTED,)


@pytest.mark.parametrize("duration_ms", [100, 299])
def test_short_voice_does_not_barge_in(duration_ms: int) -> None:
    policy = TurnPolicy()
    assert feed_ms(policy, 0, duration_ms, voiced=True, ai_playing=True) == ()


@pytest.mark.parametrize("duration_ms", [300, 301])
def test_300ms_or_more_confirms_barge_in(duration_ms: int) -> None:
    policy = TurnPolicy()
    assert feed_ms(policy, 0, duration_ms, voiced=True, ai_playing=True) == (
        TurnEventKind.BARGE_IN_CONFIRMED,
    )


def test_one_ms_silence_breaks_continuous_barge_in_candidate() -> None:
    policy = TurnPolicy()
    assert feed_ms(policy, 0, 150, voiced=True, ai_playing=True) == ()
    assert feed_ms(policy, 150, 151, voiced=False, ai_playing=True) == ()
    assert feed_ms(policy, 151, 301, voiced=True, ai_playing=True) == ()


def test_349ms_silence_does_not_end_but_350ms_does() -> None:
    policy = TurnPolicy()
    assert feed_ms(policy, 0, 100, voiced=True, ai_playing=False) == (TurnEventKind.TURN_STARTED,)
    assert feed_ms(policy, 100, 449, voiced=False, ai_playing=False) == ()
    assert feed_ms(policy, 449, 450, voiced=False, ai_playing=False) == (TurnEventKind.TURN_ENDED,)


def test_voice_before_350ms_continues_same_turn() -> None:
    policy = TurnPolicy()
    feed_ms(policy, 0, 100, voiced=True, ai_playing=False)
    assert feed_ms(policy, 100, 449, voiced=False, ai_playing=False) == ()
    assert feed_ms(policy, 449, 500, voiced=True, ai_playing=False) == ()
    assert policy.turn_active


def test_ai_stops_during_candidate_uses_idle_threshold() -> None:
    policy = TurnPolicy()
    assert feed_ms(policy, 0, 150, voiced=True, ai_playing=True) == ()
    assert feed_ms(policy, 150, 151, voiced=True, ai_playing=False) == (TurnEventKind.TURN_STARTED,)
