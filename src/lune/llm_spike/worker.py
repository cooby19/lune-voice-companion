"""Host supervisor for the isolated MLX Qwen worker.

The worker is a separate process with its own runtime, so its weights never enter the
engine's address space and a stuck generation can be stopped by killing a PID this host
itself spawned. Text is filtered here rather than in the worker, so reasoning removal and
tool-call extraction stay in tested Lune code.
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

from lune.llm_spike.thinking import ThinkingFilter, ThinkingFilterResult
from lune.llm_spike.tools import ExtractedToolCall, ToolCallExtractor

PROTOCOL_VERSION: Final[int] = 1
MAX_CONTROL_BYTES: Final[int] = 256 * 1024
_PREFIX: Final[struct.Struct] = struct.Struct("!I")

STARTUP_TIMEOUT_SECONDS: Final[float] = 300.0
CANCEL_KILL_DEADLINE_SECONDS: Final[float] = 0.5

type WorkerErrorCode = Literal[
    "worker_unavailable",
    "worker_eof",
    "protocol_error",
    "model_load_failed",
    "generation_failed",
    "setup_required",
]


class WorkerError(RuntimeError):
    def __init__(self, code: WorkerErrorCode) -> None:
        super().__init__(code)
        self.code: WorkerErrorCode = code


@dataclass(frozen=True, slots=True)
class WorkerReady:
    python_version: str
    mlx_lm_version: str
    load_ms: float
    enable_thinking_supported: bool


@dataclass(frozen=True, slots=True)
class GenerationOutcome:
    """Everything one generation produced, already filtered for downstream use."""

    generation_id: int
    status: Literal["completed", "cancelled", "error"]
    text: str = field(repr=False, default="")
    tool_calls: tuple[ExtractedToolCall, ...] = field(repr=False, default=())
    thinking: ThinkingFilterResult | None = field(repr=False, default=None)
    first_token_ms: float | None = None
    first_sentence_ms: float | None = None
    total_ms: float | None = None
    prompt_tokens: int | None = None
    generation_tokens: int | None = None
    generation_tps: float | None = None
    peak_memory_bytes: int | None = None
    finish_reason: str | None = None
    events_after_cancel: int = 0


def worker_environment(*, model_dir: Path, temp_root: Path) -> Mapping[str, str]:
    """Allowlist only. No API key, no user site, no implicit network fetch."""

    return {
        "HOME": str(temp_root),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(temp_root),
        "XDG_CACHE_HOME": str(temp_root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "LUNE_QWEN_MODEL_DIR": str(model_dir),
    }


class QwenWorkerHost:
    """Spawn one worker, fence generations, and stop it by PID when soft cancel is slow."""

    def __init__(
        self,
        *,
        python_executable: Path,
        worker_script: Path,
        model_dir: Path,
    ) -> None:
        self._python_executable = python_executable
        self._worker_script = worker_script
        self._model_dir = model_dir
        self._process: asyncio.subprocess.Process | None = None
        self._temp_root: Path | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._ready: WorkerReady | None = None
        self._current_generation = 0

    @property
    def ready(self) -> WorkerReady | None:
        return self._ready

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    async def start(self) -> WorkerReady:
        if not self._worker_script.is_file() or not self._python_executable.is_file():
            raise WorkerError("setup_required")
        temp_root = Path(tempfile.mkdtemp(prefix="lune-qwen-worker-"))
        temp_root.chmod(0o700)
        try:
            process = await asyncio.create_subprocess_exec(
                str(self._python_executable),
                "-I",
                str(self._worker_script),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=dict(worker_environment(model_dir=self._model_dir, temp_root=temp_root)),
            )
        except OSError as error:
            raise WorkerError("worker_unavailable") from error

        self._process = process
        self._temp_root = temp_root
        self._stderr_task = asyncio.create_task(
            _discard_stderr(process.stderr), name="qwen-stderr-drain"
        )
        frame = await asyncio.wait_for(self._receive(), timeout=STARTUP_TIMEOUT_SECONDS)
        if frame.get("type") == "error":
            raise WorkerError("model_load_failed")
        if frame.get("type") != "ready":
            raise WorkerError("protocol_error")
        self._ready = WorkerReady(
            python_version=str(frame.get("python_version", "")),
            mlx_lm_version=str(frame.get("mlx_lm_version", "")),
            load_ms=float(frame.get("load_ms", 0.0)),
            enable_thinking_supported=bool(frame.get("enable_thinking_supported", False)),
        )
        return self._ready

    async def generate(
        self,
        *,
        generation_id: int,
        request_id: str,
        messages: Sequence[Mapping[str, str]],
        tools: Sequence[Mapping[str, Any]] | None = None,
        max_tokens: int = 192,
        cancel_after_first_token: bool = False,
    ) -> GenerationOutcome:
        """Run one generation, dropping anything that arrives for a stale generation."""

        self._current_generation = generation_id
        await self._send(
            {
                "type": "generate",
                "request_id": request_id,
                "generation_id": generation_id,
                "messages": [dict(message) for message in messages],
                "tools": [dict(tool) for tool in tools] if tools else None,
                "max_tokens": max_tokens,
            }
        )
        return await self._collect(
            generation_id=generation_id,
            cancel_after_first_token=cancel_after_first_token,
        )

    async def _collect(
        self,
        *,
        generation_id: int,
        cancel_after_first_token: bool,
    ) -> GenerationOutcome:
        thinking = ThinkingFilter()
        extractor = ToolCallExtractor()
        visible: list[str] = []
        tool_calls: list[ExtractedToolCall] = []
        usage: dict[str, Any] = {}
        status: Literal["completed", "cancelled", "error"] = "completed"
        started = time.perf_counter()
        first_sentence_ms: float | None = None
        cancel_sent_at: float | None = None
        events_after_cancel = 0

        while True:
            frame = await self._receive()
            kind = frame.get("type")
            frame_generation = frame.get("generation_id")
            if isinstance(frame_generation, int) and frame_generation != self._current_generation:
                continue
            if kind == "token":
                if cancel_sent_at is not None:
                    events_after_cancel += 1
                filtered = thinking.feed(str(frame.get("text", "")))
                extracted = extractor.feed(filtered.text)
                visible.append(extracted.text)
                tool_calls.extend(extracted.tool_calls)
                if first_sentence_ms is None and _has_sentence("".join(visible)):
                    first_sentence_ms = (time.perf_counter() - started) * 1000.0
                if cancel_after_first_token and cancel_sent_at is None:
                    cancel_sent_at = time.perf_counter()
                    await self._send({"type": "cancel", "generation_id": generation_id})
            elif kind == "usage":
                usage = dict(frame)
            elif kind == "done":
                raw_status = frame.get("status")
                status = "cancelled" if raw_status == "cancelled" else "completed"
                break
            elif kind == "error":
                status = "error"
                break

        tail = thinking.finish()
        final = extractor.feed(tail.text)
        visible.append(final.text)
        tool_calls.extend(final.tool_calls)
        closing = extractor.finish()
        visible.append(closing.text)
        tool_calls.extend(closing.tool_calls)

        return GenerationOutcome(
            generation_id=generation_id,
            status=status,
            text="".join(visible),
            tool_calls=tuple(tool_calls),
            thinking=tail,
            first_token_ms=_optional_float(usage.get("first_token_ms")),
            first_sentence_ms=first_sentence_ms,
            total_ms=_optional_float(usage.get("total_ms")),
            prompt_tokens=_optional_int(usage.get("prompt_tokens")),
            generation_tokens=_optional_int(usage.get("generation_tokens")),
            generation_tps=_optional_float(usage.get("generation_tps")),
            peak_memory_bytes=_optional_int(usage.get("peak_memory_bytes")),
            finish_reason=_optional_str(usage.get("finish_reason")),
            events_after_cancel=events_after_cancel,
        )

    async def cancel(self, generation_id: int) -> None:
        await self._send({"type": "cancel", "generation_id": generation_id})

    def advance_generation(self) -> int:
        self._current_generation += 1
        return self._current_generation

    async def close(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            await self._send({"type": "close"})
        except WorkerError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=CANCEL_KILL_DEADLINE_SECONDS * 4)
        except TimeoutError:
            await self.terminate()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        self._process = None

    async def terminate(self) -> None:
        """Kill only the PID this host spawned, never a scanned or inherited one."""

        process = self._process
        if process is None or process.returncode is not None:
            return
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()

    async def _send(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise WorkerError("worker_unavailable")
        body = dict(payload)
        body["protocol_version"] = PROTOCOL_VERSION
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_CONTROL_BYTES:
            raise WorkerError("protocol_error")
        process.stdin.write(_PREFIX.pack(len(encoded)) + encoded)
        await process.stdin.drain()

    async def _receive(self) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdout is None:
            raise WorkerError("worker_unavailable")
        try:
            prefix = await process.stdout.readexactly(_PREFIX.size)
        except asyncio.IncompleteReadError as error:
            raise WorkerError("worker_eof") from error
        (length,) = _PREFIX.unpack(prefix)
        if length == 0 or length > MAX_CONTROL_BYTES:
            raise WorkerError("protocol_error")
        try:
            body = await process.stdout.readexactly(length)
        except asyncio.IncompleteReadError as error:
            raise WorkerError("worker_eof") from error
        try:
            frame = json.loads(body)
        except ValueError as error:
            raise WorkerError("protocol_error") from error
        if not isinstance(frame, dict) or frame.get("protocol_version") != PROTOCOL_VERSION:
            raise WorkerError("protocol_error")
        return frame


async def _discard_stderr(stream: asyncio.StreamReader | None) -> None:
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            return


def _has_sentence(text: str) -> bool:
    return any(character in text for character in "。\uff01\uff1f!?…")


def _optional_float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def default_runtime_python(support_root: Path) -> Path:
    return support_root / "models" / "qwen-runtime" / "bin" / "python"


def worker_script_path() -> Path:
    return Path(os.path.dirname(os.path.abspath(__file__))) / "qwen_worker.py"
