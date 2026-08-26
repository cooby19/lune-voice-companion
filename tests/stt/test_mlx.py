from __future__ import annotations

import asyncio
import struct
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from lune.audio.types import AudioSpan
from lune.stt.contracts import (
    FinalTranscript,
    LanguageHint,
    STTEvent,
    STTFailure,
    TranscriptionRequest,
)
from lune.stt.mlx import LuneFinalOnlySTTService, _default_inference, build_mlx_stt


def _request(
    request_id: str,
    generation_id: int,
    *,
    pcm: bytes = b"\x00\x00" * 160,
    language_hint: LanguageHint | None = None,
) -> TranscriptionRequest:
    span = AudioSpan(
        pcm=pcm,
        start_sample=0,
        end_sample=len(pcm) // 2,
        generation_id=generation_id,
    )
    return TranscriptionRequest(
        request_id=request_id,
        generation_id=generation_id,
        audio=span,
        language_hint=language_hint,
    )


async def _wait_for_count(events: list[STTEvent], count: int, changed: asyncio.Event) -> None:
    async with asyncio.timeout(2):
        while len(events) < count:
            changed.clear()
            if len(events) < count:
                await changed.wait()


@pytest.mark.parametrize("old_fails", [False, True])
async def test_generation_epoch_discards_late_result_and_error(old_fails: bool) -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    calls: list[str] = []
    events: list[STTEvent] = []
    changed = asyncio.Event()

    def inference(request: TranscriptionRequest, model_root: Path) -> str:
        del model_root
        calls.append(request.request_id)
        if request.generation_id == 7:
            loop.call_soon_threadsafe(started.set)
            if not release.wait(timeout=2):
                raise RuntimeError("barrier timeout")
            if old_fails:
                raise RuntimeError("private inference detail")
        return f"final:{request.request_id}"

    async def emit(event: STTEvent) -> None:
        events.append(event)
        changed.set()

    service = LuneFinalOnlySTTService(
        generation_id=7,
        emit=emit,
        model_root=Path("/verified/local/model"),
        inference=inference,
    )
    assert service.submit(_request("old", 7))
    await asyncio.wait_for(started.wait(), timeout=2)

    event_loop_was_responsive = False

    def heartbeat() -> None:
        nonlocal event_loop_was_responsive
        event_loop_was_responsive = True

    loop.call_soon(heartbeat)
    await asyncio.sleep(0)
    assert event_loop_was_responsive

    assert service.submit(_request("old-pending", 7))
    assert service.pending_count == 1
    service.set_generation(8)
    assert service.pending_count == 0
    assert not service.submit(_request("stale-at-acceptance", 7))
    assert service.submit(_request("current", 8))
    release.set()
    await _wait_for_count(events, 1, changed)
    assert calls == ["old", "current"]
    assert events == [FinalTranscript(request_id="current", generation_id=8, text="final:current")]
    assert all(isinstance(event, (FinalTranscript, STTFailure)) for event in events)
    await service.close()


async def test_pending_slot_is_bounded_and_latest_wins() -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    calls: list[str] = []
    events: list[STTEvent] = []
    changed = asyncio.Event()

    def inference(request: TranscriptionRequest, model_root: Path) -> str:
        del model_root
        calls.append(request.request_id)
        if request.request_id == "running":
            loop.call_soon_threadsafe(started.set)
            if not release.wait(timeout=2):
                raise RuntimeError("barrier timeout")
        return request.request_id

    async def emit(event: STTEvent) -> None:
        events.append(event)
        changed.set()

    service = LuneFinalOnlySTTService(
        generation_id=9,
        emit=emit,
        model_root=Path("/verified/local/model"),
        inference=inference,
    )
    assert service.submit(_request("running", 9))
    await asyncio.wait_for(started.wait(), timeout=2)
    assert service.submit(_request("superseded", 9))
    assert service.submit(_request("latest", 9))
    assert service.pending_count == 1
    release.set()
    await _wait_for_count(events, 2, changed)
    assert calls == ["running", "latest"]
    assert [event.request_id for event in events] == ["running", "latest"]
    await service.close()


async def test_close_does_not_wait_for_native_thread_or_emit_afterward() -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    events: list[STTEvent] = []
    changed = asyncio.Event()

    def inference(request: TranscriptionRequest, model_root: Path) -> str:
        del request, model_root
        loop.call_soon_threadsafe(started.set)
        if not release.wait(timeout=2):
            raise RuntimeError("barrier timeout")
        return "late"

    async def emit(event: STTEvent) -> None:
        events.append(event)
        changed.set()

    service = LuneFinalOnlySTTService(
        generation_id=3,
        emit=emit,
        model_root=Path("/verified/local/model"),
        inference=inference,
    )
    assert service.submit(_request("running", 3))
    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.wait_for(service.close(), timeout=0.2)
    assert not service.worker_active
    assert not service.submit(_request("after-close", 3))
    release.set()
    await asyncio.sleep(0.01)
    assert events == []


async def test_missing_manifest_and_optional_dependency_are_finite_setup_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[STTEvent] = []
    changed = asyncio.Event()

    async def emit(event: STTEvent) -> None:
        events.append(event)
        changed.set()

    missing_manifest = tmp_path / "private" / "manifest.json"
    missing_service = build_mlx_stt(
        manifest_path=missing_manifest,
        generation_id=1,
        emit=emit,
    )
    assert missing_service.submit(_request("missing-model", 1))
    await _wait_for_count(events, 1, changed)
    assert events == [
        STTFailure(request_id="missing-model", generation_id=1, code="setup_required")
    ]
    assert str(tmp_path) not in repr(events[0])
    await missing_service.close()

    events.clear()
    changed.clear()

    def missing_import(name: str) -> object:
        assert name == "mlx_whisper"
        raise ModuleNotFoundError(name)

    monkeypatch.setattr("lune.stt.mlx.importlib.import_module", missing_import)
    dependency_service = LuneFinalOnlySTTService(
        generation_id=2,
        emit=emit,
        model_root=tmp_path,
    )
    assert dependency_service.submit(_request("missing-dependency", 2))
    await _wait_for_count(events, 1, changed)
    assert events == [
        STTFailure(request_id="missing-dependency", generation_id=2, code="setup_required")
    ]
    await dependency_service.close()


def test_real_adapter_passes_normalized_pcm_and_only_a_local_model_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def transcribe(audio: np.ndarray, **options: object) -> dict[str, str]:
        captured["audio"] = audio
        captured["options"] = options
        return {"text": " 最終文字 "}

    module = SimpleNamespace(transcribe=transcribe)
    monkeypatch.setattr("lune.stt.mlx.importlib.import_module", lambda name: module)
    pcm = struct.pack("<hhh", -32768, 0, 32767)
    request = _request("real", 4, pcm=pcm, language_hint="zh")
    assert _default_inference(request, tmp_path) == "最終文字"
    audio = captured["audio"]
    options = captured["options"]
    assert isinstance(audio, np.ndarray)
    np.testing.assert_allclose(audio, np.array([-1.0, 0.0, 32767 / 32768], dtype=np.float32))
    assert options == {
        "path_or_hf_repo": str(tmp_path),
        "verbose": None,
        "language": "zh",
    }
