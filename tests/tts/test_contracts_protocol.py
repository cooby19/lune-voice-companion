from __future__ import annotations

import struct

import pytest

from lune.tts.contracts import PCMChunk, TTSRequest
from lune.tts.protocol import (
    MAX_FRAME_BYTES,
    ControlFrame,
    FrameDecoder,
    PCMFrame,
    WorkerProtocolError,
    encode_control,
    encode_pcm,
)


def test_private_tts_payloads_are_omitted_from_repr() -> None:
    request = TTSRequest("request-1", 7, "private utterance", "zh")
    chunk = PCMChunk(7, 32_000, 1, b"\x00\x00")

    assert "private utterance" not in repr(request)
    assert "\\x00" not in repr(chunk)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"request_id": "", "generation_id": 1, "text": "ok"},
        {"request_id": "r", "generation_id": -1, "text": "ok"},
        {"request_id": "r", "generation_id": 1, "text": "   "},
    ],
)
def test_request_validation(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        TTSRequest(**kwargs)  # type: ignore[arg-type]


def test_fragmented_and_coalesced_frames_decode_incrementally() -> None:
    control = encode_control(
        ControlFrame(type="synthesize", request_id="r", generation_id=9, text="hello")
    )
    pcm = encode_pcm(PCMFrame(0, PCMChunk(9, 32_000, 1, b"\x01\x00\x02\x00")))
    decoder = FrameDecoder()

    assert decoder.feed(control[:2]) == ()
    assert decoder.feed(control[2:-1]) == ()
    first = decoder.feed(control[-1:] + pcm)

    assert first == (
        ControlFrame(type="synthesize", request_id="r", generation_id=9, text="hello"),
        PCMFrame(0, PCMChunk(9, 32_000, 1, b"\x01\x00\x02\x00")),
    )


def test_protocol_rejects_oversized_and_unknown_frames() -> None:
    decoder = FrameDecoder()
    with pytest.raises(WorkerProtocolError, match="invalid_frame_length"):
        decoder.feed(struct.pack("!I", MAX_FRAME_BYTES + 1))

    decoder = FrameDecoder()
    with pytest.raises(WorkerProtocolError, match="unknown_frame_kind"):
        decoder.feed(struct.pack("!I", 2) + b"\xffx")


def test_protocol_rejects_unexpected_control_fields() -> None:
    body = b'\x01{"protocol_version":1,"type":"ready","secret":"no"}'
    decoder = FrameDecoder()
    with pytest.raises(WorkerProtocolError, match="unexpected_control_field"):
        decoder.feed(struct.pack("!I", len(body)) + body)
