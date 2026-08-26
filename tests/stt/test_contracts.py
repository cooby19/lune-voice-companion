from __future__ import annotations

import pytest

from lune.audio.types import AudioSpan
from lune.stt.contracts import FinalTranscript, TranscriptionRequest


def _audio(*, generation_id: int = 7, sample_rate: int = 16_000) -> AudioSpan:
    return AudioSpan(
        pcm=b"private-pcm".ljust(320, b"\x00"),
        start_sample=0,
        end_sample=160,
        generation_id=generation_id,
        sample_rate=sample_rate,
    )


def test_contract_repr_hides_pcm_and_transcript() -> None:
    audio = _audio()
    request = TranscriptionRequest(request_id="turn-7", generation_id=7, audio=audio)
    final = FinalTranscript(request_id="turn-7", generation_id=7, text="私人逐字稿")
    assert "private-pcm" not in repr(audio)
    assert "private-pcm" not in repr(request)
    assert "私人逐字稿" not in repr(final)


def test_request_requires_matching_generation_and_fixed_audio_format() -> None:
    with pytest.raises(ValueError, match="generation IDs"):
        TranscriptionRequest(request_id="turn", generation_id=8, audio=_audio())
    with pytest.raises(ValueError, match="16 kHz mono"):
        TranscriptionRequest(
            request_id="turn",
            generation_id=7,
            audio=_audio(sample_rate=48_000),
        )
