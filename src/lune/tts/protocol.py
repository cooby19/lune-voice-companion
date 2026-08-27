"""Bounded length-prefixed control and PCM protocol for the GPT worker."""

from __future__ import annotations

import asyncio
import json
import struct
from dataclasses import dataclass, field
from typing import Final, Literal, cast

from lune.tts.contracts import PCMChunk

PROTOCOL_VERSION: Final[int] = 1
MAX_CONTROL_BYTES: Final[int] = 64 * 1024
MAX_PCM_BYTES: Final[int] = 1024 * 1024
MAX_FRAME_BYTES: Final[int] = MAX_PCM_BYTES + 64

_CONTROL_KIND: Final[int] = 1
_PCM_KIND: Final[int] = 2
_PREFIX = struct.Struct("!I")
_PCM_HEADER = struct.Struct("!QIIH")

ControlType = Literal["ready", "synthesize", "cancel", "done", "error", "close"]


class WorkerProtocolError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ControlFrame:
    type: ControlType
    request_id: str | None = None
    generation_id: int | None = None
    sequence: int | None = None
    code: str | None = None
    text: str | None = field(default=None, repr=False)
    language_hint: str | None = None
    python_version: str | None = None


@dataclass(frozen=True, slots=True)
class PCMFrame:
    sequence: int
    chunk: PCMChunk


type WorkerFrame = ControlFrame | PCMFrame


def encode_control(frame: ControlFrame) -> bytes:
    payload: dict[str, object] = {
        "protocol_version": PROTOCOL_VERSION,
        "type": frame.type,
    }
    for key in (
        "request_id",
        "generation_id",
        "sequence",
        "code",
        "text",
        "language_hint",
        "python_version",
    ):
        value = getattr(frame, key)
        if value is not None:
            payload[key] = value
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_CONTROL_BYTES:
        raise WorkerProtocolError("control_frame_too_large")
    body = bytes((_CONTROL_KIND,)) + encoded
    return _PREFIX.pack(len(body)) + body


def encode_pcm(frame: PCMFrame) -> bytes:
    chunk = frame.chunk
    if len(chunk.data) > MAX_PCM_BYTES:
        raise WorkerProtocolError("pcm_frame_too_large")
    body = (
        bytes((_PCM_KIND,))
        + _PCM_HEADER.pack(
            chunk.generation_id,
            frame.sequence,
            chunk.sample_rate,
            chunk.channels,
        )
        + chunk.data
    )
    return _PREFIX.pack(len(body)) + body


def decode_body(body: bytes) -> WorkerFrame:
    if not body:
        raise WorkerProtocolError("empty_frame")
    kind, payload = body[0], body[1:]
    if kind == _CONTROL_KIND:
        if len(payload) > MAX_CONTROL_BYTES:
            raise WorkerProtocolError("control_frame_too_large")
        return _decode_control(payload)
    if kind == _PCM_KIND:
        if len(payload) > MAX_PCM_BYTES + _PCM_HEADER.size:
            raise WorkerProtocolError("pcm_frame_too_large")
        return _decode_pcm(payload)
    raise WorkerProtocolError("unknown_frame_kind")


def _decode_control(payload: bytes) -> ControlFrame:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkerProtocolError("invalid_control_json") from error
    if not isinstance(value, dict) or value.get("protocol_version") != PROTOCOL_VERSION:
        raise WorkerProtocolError("protocol_version_mismatch")
    allowed = {
        "protocol_version",
        "type",
        "request_id",
        "generation_id",
        "sequence",
        "code",
        "text",
        "language_hint",
        "python_version",
    }
    if not set(value).issubset(allowed):
        raise WorkerProtocolError("unexpected_control_field")
    frame_type = value.get("type")
    if frame_type not in {"ready", "synthesize", "cancel", "done", "error", "close"}:
        raise WorkerProtocolError("invalid_control_type")
    request_id = _optional_string(value, "request_id", 128)
    code = _optional_string(value, "code", 64)
    text = _optional_string(value, "text", 8_000)
    language_hint = _optional_string(value, "language_hint", 16)
    python_version = _optional_string(value, "python_version", 32)
    generation_id = _optional_nonnegative_int(value, "generation_id")
    sequence = _optional_nonnegative_int(value, "sequence")
    return ControlFrame(
        type=cast(ControlType, frame_type),
        request_id=request_id,
        generation_id=generation_id,
        sequence=sequence,
        code=code,
        text=text,
        language_hint=language_hint,
        python_version=python_version,
    )


def _optional_string(value: dict[object, object], key: str, maximum: int) -> str | None:
    raw = value.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw or len(raw) > maximum:
        raise WorkerProtocolError(f"invalid_{key}")
    return raw


def _optional_nonnegative_int(value: dict[object, object], key: str) -> int | None:
    raw = value.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw < 2**63:
        raise WorkerProtocolError(f"invalid_{key}")
    return raw


def _decode_pcm(payload: bytes) -> PCMFrame:
    if len(payload) <= _PCM_HEADER.size:
        raise WorkerProtocolError("invalid_pcm_frame")
    generation_id, sequence, sample_rate, channels = _PCM_HEADER.unpack_from(payload)
    try:
        chunk = PCMChunk(
            generation_id=generation_id,
            sample_rate=sample_rate,
            channels=channels,
            data=payload[_PCM_HEADER.size :],
        )
    except ValueError as error:
        raise WorkerProtocolError("invalid_pcm_frame") from error
    return PCMFrame(sequence=sequence, chunk=chunk)


class FrameDecoder:
    """Incremental decoder used by deterministic fragmentation tests."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> tuple[WorkerFrame, ...]:
        self._buffer.extend(data)
        frames: list[WorkerFrame] = []
        while len(self._buffer) >= _PREFIX.size:
            (length,) = _PREFIX.unpack_from(self._buffer)
            if length == 0 or length > MAX_FRAME_BYTES:
                raise WorkerProtocolError("invalid_frame_length")
            total = _PREFIX.size + length
            if len(self._buffer) < total:
                break
            body = bytes(self._buffer[_PREFIX.size : total])
            del self._buffer[:total]
            frames.append(decode_body(body))
        return tuple(frames)


async def read_frame(reader: asyncio.StreamReader) -> WorkerFrame:
    try:
        prefix = await reader.readexactly(_PREFIX.size)
    except asyncio.IncompleteReadError as error:
        raise WorkerProtocolError("worker_eof") from error
    (length,) = _PREFIX.unpack(prefix)
    if length == 0 or length > MAX_FRAME_BYTES:
        raise WorkerProtocolError("invalid_frame_length")
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as error:
        raise WorkerProtocolError("truncated_frame") from error
    return decode_body(body)
