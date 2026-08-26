from __future__ import annotations

import struct

import pytest

from lune.audio.preroll import PreRollBuffer
from lune.audio.types import AudioSpan


def pcm_for(start: int, end: int) -> bytes:
    return b"".join(struct.pack("<h", sample % 32_767) for sample in range(start, end))


def span(start: int, end: int, generation_id: int = 4) -> AudioSpan:
    return AudioSpan(
        pcm=pcm_for(start, end),
        start_sample=start,
        end_sample=end,
        generation_id=generation_id,
    )


def test_capacity_must_cover_preroll_and_barge_confirmation() -> None:
    with pytest.raises(ValueError):
        PreRollBuffer(capacity_ms=649)


def test_wraparound_capture_has_no_gap_or_duplicate() -> None:
    ring = PreRollBuffer(capacity_ms=700)
    for start in range(0, 20_000, 800):
        ring.append(span(start, start + 800))
    capture = ring.capture(voice_onset_sample=15_200, confirmation_sample=20_000)
    assert capture.audio.start_sample == 9_600
    assert capture.audio.end_sample == 20_000
    assert capture.audio.pcm == pcm_for(9_600, 20_000)
    assert not capture.pre_roll_truncated


def test_stream_start_marks_truncated_preroll() -> None:
    ring = PreRollBuffer()
    ring.append(span(0, 5_000))
    capture = ring.capture(voice_onset_sample=1_000, confirmation_sample=5_000)
    assert capture.audio.start_sample == 0
    assert capture.requested_start_sample == 0
    assert capture.pre_roll_truncated


def test_generation_change_clears_old_audio() -> None:
    ring = PreRollBuffer()
    ring.append(span(0, 5_000, generation_id=1))
    ring.append(span(5_000, 10_000, generation_id=2))
    assert ring.start_sample == 5_000
    assert ring.end_sample == 10_000
