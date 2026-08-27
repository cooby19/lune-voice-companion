"""Standalone Python 3.10 GPT-SoVITS worker.

This file deliberately imports no other Lune module so the isolated runtime does
not need the Python 3.12 core package. Stdout is reserved for framed protocol
bytes; upstream prints are redirected to stderr and discarded by the host.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
import sys
import threading
from pathlib import Path
from typing import Any, BinaryIO

PROTOCOL_VERSION = 1
PINNED_UPSTREAM_COMMIT = "48b1a0169a28582a8984402f82cf438d3bfa6aca"
MAX_CONTROL_BYTES = 64 * 1024
MAX_PCM_BYTES = 1024 * 1024
MAX_FRAME_BYTES = MAX_PCM_BYTES + 64

_CONTROL_KIND = 1
_PCM_KIND = 2
_PREFIX = struct.Struct("!I")
_PCM_HEADER = struct.Struct("!QIIH")
_PROTOCOL_OUT = sys.stdout.buffer
sys.stdout = sys.stderr
_WRITE_LOCK = threading.Lock()


def _write_control(frame_type: str, **values: object) -> None:
    payload = {"protocol_version": PROTOCOL_VERSION, "type": frame_type}
    payload.update(values)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_CONTROL_BYTES:
        raise RuntimeError("control_frame_too_large")
    _write_body(bytes((_CONTROL_KIND,)) + encoded)


def _write_pcm(generation_id: int, sequence: int, sample_rate: int, data: bytes) -> None:
    if not data or len(data) > MAX_PCM_BYTES or len(data) % 2:
        raise RuntimeError("invalid_pcm")
    body = bytes((_PCM_KIND,)) + _PCM_HEADER.pack(generation_id, sequence, sample_rate, 1) + data
    _write_body(body)


def _write_body(body: bytes) -> None:
    with _WRITE_LOCK:
        _PROTOCOL_OUT.write(_PREFIX.pack(len(body)))
        _PROTOCOL_OUT.write(body)
        _PROTOCOL_OUT.flush()


def _read_exactly(stream: BinaryIO, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)


def _read_control(stream: BinaryIO) -> dict[str, object] | None:
    prefix = _read_exactly(stream, _PREFIX.size)
    if not prefix:
        return None
    if len(prefix) != _PREFIX.size:
        raise RuntimeError("truncated_prefix")
    (length,) = _PREFIX.unpack(prefix)
    if length < 2 or length > MAX_FRAME_BYTES:
        raise RuntimeError("invalid_frame_length")
    body = _read_exactly(stream, length)
    if len(body) != length or body[0] != _CONTROL_KIND:
        raise RuntimeError("invalid_control_frame")
    try:
        value = json.loads(body[1:])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("invalid_control_json") from error
    if not isinstance(value, dict) or value.get("protocol_version") != PROTOCOL_VERSION:
        raise RuntimeError("protocol_version_mismatch")
    return value


def _secure_json(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise RuntimeError("manifest_invalid")
        if metadata.st_mode & 0o077 or metadata.st_size > MAX_CONTROL_BYTES:
            raise RuntimeError("manifest_invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            value = json.load(handle)
    finally:
        os.close(descriptor)
    if not isinstance(value, dict):
        raise RuntimeError("manifest_invalid")
    return value


def _private_asset(root: Path, value: object) -> Path:
    if not isinstance(value, dict) or set(value) != {"relative_path", "sha256"}:
        raise RuntimeError("manifest_invalid")
    relative_path = value.get("relative_path")
    expected_digest = value.get("sha256")
    if not isinstance(relative_path, str) or not isinstance(expected_digest, str):
        raise RuntimeError("manifest_invalid")
    parts = relative_path.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts) or "\\" in relative_path:
        raise RuntimeError("manifest_invalid")
    path = root.joinpath(*parts)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise RuntimeError("asset_invalid")
        if metadata.st_mode & 0o077:
            raise RuntimeError("asset_invalid")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    finally:
        os.close(descriptor)
    if digest.hexdigest() != expected_digest:
        raise RuntimeError("asset_invalid")
    return path


def _load_private_configuration() -> tuple[Path, Path, Path, str, str, Path]:
    runtime_root = Path(_required_environment("LUNE_GPT_RUNTIME_ROOT"))
    voice_root = Path(_required_environment("LUNE_GPT_VOICE_ROOT"))
    manifest_path = Path(_required_environment("LUNE_GPT_MANIFEST"))
    revision_path = runtime_root / ".lune-revision"
    revision = revision_path.read_text(encoding="ascii").strip()
    if revision != PINNED_UPSTREAM_COMMIT:
        raise RuntimeError("runtime_revision_mismatch")
    manifest = _secure_json(manifest_path)
    if manifest.get("schema_version") != 1 or manifest.get("upstream_commit") != revision:
        raise RuntimeError("manifest_revision_mismatch")
    assets = manifest.get("assets")
    reference = manifest.get("reference")
    if not isinstance(assets, dict) or not isinstance(reference, dict):
        raise RuntimeError("manifest_invalid")
    if set(assets) != {"gpt_checkpoint", "sovits_checkpoint", "reference_audio"}:
        raise RuntimeError("manifest_invalid")
    gpt = _private_asset(voice_root, assets["gpt_checkpoint"])
    sovits = _private_asset(voice_root, assets["sovits_checkpoint"])
    reference_audio = _private_asset(voice_root, assets["reference_audio"])
    prompt_language = reference.get("language")
    prompt_text = reference.get("prompt_text")
    if prompt_language not in {"zh", "en", "auto"}:
        raise RuntimeError("manifest_invalid")
    if not isinstance(prompt_text, str) or not 1 <= len(prompt_text) <= 2_000:
        raise RuntimeError("manifest_invalid")
    return gpt, sovits, reference_audio, prompt_language, prompt_text, runtime_root


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError("environment_missing")
    return value


def _load_pipeline() -> tuple[Any, Path, str, str]:
    gpt, sovits, reference_audio, prompt_language, prompt_text, runtime_root = (
        _load_private_configuration()
    )
    sys.path.insert(0, str(runtime_root))
    sys.path.insert(0, str(runtime_root / "GPT_SoVITS"))
    from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config  # type: ignore[import-not-found]

    pretrained = runtime_root / "GPT_SoVITS" / "pretrained_models"
    config = TTS_Config(
        {
            "custom": {
                "device": "mps",
                "is_half": False,
                "version": "v2",
                "t2s_weights_path": str(gpt),
                "vits_weights_path": str(sovits),
                "bert_base_path": str(pretrained / "chinese-roberta-wwm-ext-large"),
                "cnhuhbert_base_path": str(pretrained / "chinese-hubert-base"),
            }
        }
    )
    return TTS(config), reference_audio, prompt_language, prompt_text


def _language(value: object) -> str:
    return str(value) if value in {"zh", "en", "auto"} else "auto"


def _synthesize(
    pipeline: Any,
    reference_audio: Path,
    prompt_language: str,
    prompt_text: str,
    request_id: str,
    generation_id: int,
    text: str,
    language_hint: str,
    cancel_event: threading.Event,
) -> None:
    sequence = 0
    try:
        inputs = {
            "text": text,
            "text_lang": language_hint,
            "ref_audio_path": str(reference_audio),
            "prompt_text": prompt_text,
            "prompt_lang": prompt_language,
            "text_split_method": "cut5",
            "batch_size": 1,
            "split_bucket": False,
            "return_fragment": False,
            "streaming_mode": True,
            "fixed_length_chunk": False,
            "parallel_infer": False,
            "fragment_interval": 0.0,
        }
        for sample_rate, audio in pipeline.run(inputs):
            if cancel_event.is_set():
                break
            if getattr(audio.dtype, "name", "") != "int16":
                audio = audio.astype("int16")
            data = audio.tobytes(order="C")
            if not data:
                continue
            for offset in range(0, len(data), MAX_PCM_BYTES):
                if cancel_event.is_set():
                    break
                _write_pcm(
                    generation_id,
                    sequence,
                    int(sample_rate),
                    data[offset : offset + MAX_PCM_BYTES],
                )
                sequence += 1
        _write_control(
            "done",
            request_id=request_id,
            generation_id=generation_id,
            sequence=sequence,
            code="cancelled" if cancel_event.is_set() else "complete",
        )
    except Exception:
        _write_control(
            "error",
            request_id=request_id,
            generation_id=generation_id,
            code="synthesis_failed",
        )


def main() -> int:
    if sys.version_info[:2] != (3, 10):
        _write_control("error", code="python_version_invalid")
        return 2
    try:
        pipeline, reference_audio, prompt_language, prompt_text = _load_pipeline()
    except Exception:
        _write_control("error", code="worker_init_failed")
        return 3

    _write_control("ready", python_version=".".join(map(str, sys.version_info[:3])))
    active: tuple[int, threading.Event, threading.Thread] | None = None
    while True:
        try:
            frame = _read_control(sys.stdin.buffer)
        except Exception:
            return 4
        if frame is None:
            return 0
        frame_type = frame.get("type")
        if frame_type == "close":
            if active is not None and active[2].is_alive():
                active[1].set()
                pipeline.stop()
            return 0
        if frame_type == "cancel":
            generation_id = frame.get("generation_id")
            if active is not None and generation_id == active[0] and active[2].is_alive():
                active[1].set()
                pipeline.stop()
            continue
        if frame_type != "synthesize" or (active is not None and active[2].is_alive()):
            _write_control("error", code="worker_busy")
            continue
        request_id = frame.get("request_id")
        generation_id = frame.get("generation_id")
        text = frame.get("text")
        if (
            not isinstance(request_id, str)
            or not 1 <= len(request_id) <= 128
            or isinstance(generation_id, bool)
            or not isinstance(generation_id, int)
            or generation_id < 0
            or not isinstance(text, str)
            or not 1 <= len(text) <= 8_000
        ):
            _write_control("error", code="invalid_request")
            continue
        cancel_event = threading.Event()
        thread = threading.Thread(
            target=_synthesize,
            args=(
                pipeline,
                reference_audio,
                prompt_language,
                prompt_text,
                request_id,
                generation_id,
                text,
                _language(frame.get("language_hint")),
                cancel_event,
            ),
            daemon=True,
            name="lune-gpt-synthesis",
        )
        active = (generation_id, cancel_event, thread)
        thread.start()


if __name__ == "__main__":
    raise SystemExit(main())
