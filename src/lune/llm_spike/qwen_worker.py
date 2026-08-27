"""Standalone MLX Qwen worker for the local LLM spike.

This file deliberately imports no other Lune module: the isolated runtime venv holds
only `mlx-lm`, so the Python 3.12 core package is not importable there. Stdout carries
framed protocol bytes and nothing else; every other write is redirected to stderr, which
the host drains and discards.

The model is loaded from a host-verified local directory. Remote code is never trusted and
the worker never resolves a repository ID, so it cannot trigger an implicit download.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
MAX_CONTROL_BYTES = 256 * 1024
_PREFIX = struct.Struct("!I")
_PROTOCOL_OUT = sys.stdout.buffer
sys.stdout = sys.stderr
_WRITE_LOCK = threading.Lock()

_CANCEL_LOCK = threading.Lock()
_CANCELLED: set[int] = set()
_SHUTDOWN = threading.Event()


def _write(frame_type: str, **values: object) -> None:
    payload: dict[str, object] = {"protocol_version": PROTOCOL_VERSION, "type": frame_type}
    payload.update({key: value for key, value in values.items() if value is not None})
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_CONTROL_BYTES:
        raise RuntimeError("control_frame_too_large")
    with _WRITE_LOCK:
        _PROTOCOL_OUT.write(_PREFIX.pack(len(encoded)) + encoded)
        _PROTOCOL_OUT.flush()


def _read_frames(stream: Any) -> Any:
    while True:
        prefix = stream.read(_PREFIX.size)
        if not prefix or len(prefix) < _PREFIX.size:
            return
        (length,) = _PREFIX.unpack(prefix)
        if length == 0 or length > MAX_CONTROL_BYTES:
            return
        body = stream.read(length)
        if len(body) < length:
            return
        try:
            frame = json.loads(body)
        except ValueError:
            continue
        if isinstance(frame, dict) and frame.get("protocol_version") == PROTOCOL_VERSION:
            yield frame


def _mark_cancelled(generation_id: int) -> None:
    with _CANCEL_LOCK:
        _CANCELLED.add(generation_id)


def _is_cancelled(generation_id: int) -> bool:
    with _CANCEL_LOCK:
        return generation_id in _CANCELLED


def _validated_model_dir() -> Path:
    raw = os.environ.get("LUNE_QWEN_MODEL_DIR")
    if not raw:
        raise RuntimeError("model_dir_missing")
    path = Path(raw)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise RuntimeError("model_dir_invalid")
    return path


def _build_prompt(tokenizer: Any, messages: list[dict[str, str]], tools: Any) -> tuple[str, bool]:
    """Render the chat template with thinking disabled, reporting whether that was accepted."""

    kwargs: dict[str, Any] = {"add_generation_prompt": True, "tokenize": False}
    if tools:
        kwargs["tools"] = tools
    try:
        prompt = tokenizer.apply_chat_template(messages, enable_thinking=False, **kwargs)
        return str(prompt), True
    except (TypeError, ValueError):
        prompt = tokenizer.apply_chat_template(messages, **kwargs)
        return str(prompt), False


def _handle_generate(model: Any, tokenizer: Any, frame: dict[str, Any]) -> None:
    from mlx_lm.generate import stream_generate
    from mlx_lm.sample_utils import make_sampler

    request_id = str(frame.get("request_id", ""))
    generation_id = int(frame.get("generation_id", 0))
    messages = frame.get("messages") or []
    tools = frame.get("tools")
    max_tokens = int(frame.get("max_tokens", 192))

    if _is_cancelled(generation_id):
        _write("done", request_id=request_id, generation_id=generation_id, status="cancelled")
        return

    started = time.perf_counter()
    prompt, thinking_disabled = _build_prompt(tokenizer, messages, tools)
    prompt_built_at = time.perf_counter()

    sampler = make_sampler(temp=0.0)
    first_token_at: float | None = None
    index = 0
    status = "completed"
    last: Any = None

    for response in stream_generate(
        model,
        tokenizer,
        prompt,
        max_tokens=max_tokens,
        sampler=sampler,
    ):
        if _is_cancelled(generation_id):
            status = "cancelled"
            break
        if first_token_at is None:
            first_token_at = time.perf_counter()
        last = response
        if response.text:
            _write(
                "token",
                request_id=request_id,
                generation_id=generation_id,
                text=response.text,
                index=index,
            )
            index += 1

    finished = time.perf_counter()
    _write(
        "usage",
        request_id=request_id,
        generation_id=generation_id,
        prompt_build_ms=(prompt_built_at - started) * 1000.0,
        first_token_ms=((first_token_at - started) * 1000.0) if first_token_at else None,
        total_ms=(finished - started) * 1000.0,
        prompt_tokens=getattr(last, "prompt_tokens", None),
        generation_tokens=getattr(last, "generation_tokens", None),
        prompt_tps=getattr(last, "prompt_tps", None),
        generation_tps=getattr(last, "generation_tps", None),
        peak_memory_bytes=(
            int(getattr(last, "peak_memory", 0) * (1024**3)) if last is not None else None
        ),
        thinking_disabled=thinking_disabled,
        finish_reason=getattr(last, "finish_reason", None),
    )
    _write("done", request_id=request_id, generation_id=generation_id, status=status)


def _reader(commands: Any) -> None:
    for frame in _read_frames(sys.stdin.buffer):
        kind = frame.get("type")
        if kind == "cancel":
            generation_id = frame.get("generation_id")
            if isinstance(generation_id, int):
                _mark_cancelled(generation_id)
        elif kind == "close":
            _SHUTDOWN.set()
            commands.put(None)
            return
        elif kind == "generate":
            commands.put(frame)
    _SHUTDOWN.set()
    commands.put(None)


def main() -> int:
    import queue

    try:
        model_dir = _validated_model_dir()
    except RuntimeError as error:
        _write("error", code=str(error))
        return 1

    load_started = time.perf_counter()
    try:
        from mlx_lm.utils import load

        model, tokenizer = load(str(model_dir))
    except Exception:
        _write("error", code="model_load_failed")
        return 1
    load_ms = (time.perf_counter() - load_started) * 1000.0

    import mlx_lm

    probe_messages = [{"role": "user", "content": "ping"}]
    _, thinking_disabled = _build_prompt(tokenizer, probe_messages, None)

    _write(
        "ready",
        python_version=".".join(str(part) for part in sys.version_info[:3]),
        mlx_lm_version=getattr(mlx_lm, "__version__", "unknown"),
        load_ms=load_ms,
        enable_thinking_supported=thinking_disabled,
    )

    commands: Any = queue.Queue()
    thread = threading.Thread(target=_reader, args=(commands,), daemon=True)
    thread.start()

    while True:
        frame = commands.get()
        if frame is None:
            break
        try:
            _handle_generate(model, tokenizer, frame)
        except Exception:
            _write(
                "error",
                request_id=str(frame.get("request_id", "")),
                generation_id=int(frame.get("generation_id", 0)),
                code="generation_failed",
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
