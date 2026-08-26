"""Thin MLX Whisper adapter with bounded pending work and generation fences."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

import numpy as np

from lune.stt.contracts import (
    FinalTranscript,
    STTEvent,
    STTFailure,
    TranscriptionRequest,
)
from lune.stt.model_manifest import check_model_manifest


class InferenceFunction(Protocol):
    def __call__(self, request: TranscriptionRequest, model_root: Path) -> str: ...


EventSink = Callable[[STTEvent], Awaitable[None]]


class _SetupRequiredError(Exception):
    pass


def _load_mlx_whisper() -> ModuleType:
    try:
        return importlib.import_module("mlx_whisper")
    except (ImportError, ModuleNotFoundError) as error:
        raise _SetupRequiredError from error


def _default_inference(request: TranscriptionRequest, model_root: Path) -> str:
    module = _load_mlx_whisper()
    transcribe = getattr(module, "transcribe", None)
    if not callable(transcribe):
        raise _SetupRequiredError

    audio = np.frombuffer(request.audio.pcm, dtype="<i2").astype(np.float32)
    audio /= 32768.0
    options: dict[str, object] = {
        "path_or_hf_repo": str(model_root),
        "verbose": None,
    }
    if request.language_hint is not None:
        options["language"] = request.language_hint
    raw_result = transcribe(audio, **options)
    if not isinstance(raw_result, Mapping) or not isinstance(raw_result.get("text"), str):
        raise RuntimeError("MLX Whisper returned an invalid result")
    return cast(str, raw_result["text"]).strip()


class LuneFinalOnlySTTService:
    """Run at most one inference with one latest-wins pending request."""

    def __init__(
        self,
        *,
        generation_id: int,
        emit: EventSink,
        model_root: Path | None,
        inference: InferenceFunction = _default_inference,
    ) -> None:
        if generation_id < 0:
            raise ValueError("generation ID cannot be negative")
        self._generation_id = generation_id
        self._emit = emit
        self._model_root = model_root
        self._inference = inference
        self._pending: TranscriptionRequest | None = None
        self._pending_available = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def generation_id(self) -> int:
        return self._generation_id

    @property
    def pending_count(self) -> int:
        return int(self._pending is not None)

    @property
    def worker_active(self) -> bool:
        return self._worker is not None and not self._worker.done()

    def set_generation(self, generation_id: int) -> None:
        """Invalidate old work; native inference threads are allowed to finish naturally."""

        if generation_id < self._generation_id:
            raise ValueError("generation ID cannot move backwards")
        self._generation_id = generation_id
        if self._pending is not None and self._pending.generation_id != generation_id:
            self._pending = None
            self._pending_available.clear()

    def submit(self, request: TranscriptionRequest) -> bool:
        """Accept only current-generation work; the single pending slot is latest-wins."""

        if self._closed or request.generation_id != self._generation_id:
            return False
        self._pending = request
        self._pending_available.set()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="lune-stt-worker")
        return True

    async def close(self) -> None:
        """Stop Lune's asyncio worker without claiming to terminate native inference."""

        if self._closed:
            return
        self._closed = True
        self._pending = None
        self._pending_available.set()
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._worker = None

    def _is_current(self, request: TranscriptionRequest) -> bool:
        return not self._closed and request.generation_id == self._generation_id

    async def _run(self) -> None:
        while not self._closed:
            await self._pending_available.wait()
            if self._closed:
                return
            request = self._pending
            self._pending = None
            self._pending_available.clear()
            if request is None or not self._is_current(request):
                continue

            if self._model_root is None:
                event: STTEvent = STTFailure(
                    request_id=request.request_id,
                    generation_id=request.generation_id,
                    code="setup_required",
                )
            else:
                try:
                    text = await asyncio.to_thread(self._inference, request, self._model_root)
                except _SetupRequiredError:
                    if not self._is_current(request):
                        continue
                    event = STTFailure(
                        request_id=request.request_id,
                        generation_id=request.generation_id,
                        code="setup_required",
                    )
                except Exception:
                    if not self._is_current(request):
                        continue
                    event = STTFailure(
                        request_id=request.request_id,
                        generation_id=request.generation_id,
                        code="inference_failed",
                    )
                else:
                    if not self._is_current(request):
                        continue
                    event = FinalTranscript(
                        request_id=request.request_id,
                        generation_id=request.generation_id,
                        text=text,
                    )

            if not self._is_current(request):
                continue
            await self._emit(event)


def build_mlx_stt(
    *,
    manifest_path: Path,
    generation_id: int,
    emit: EventSink,
    inference: InferenceFunction = _default_inference,
) -> LuneFinalOnlySTTService:
    """Build a service that can only give MLX a verified local model directory."""

    check = check_model_manifest(manifest_path)
    model_root = check.manifest.model_root if check.ready and check.manifest is not None else None
    return LuneFinalOnlySTTService(
        generation_id=generation_id,
        emit=emit,
        model_root=model_root,
        inference=inference,
    )
