"""Run one authorized, sanitized, entirely local physical voice smoke scenario."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import multiprocessing
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np

from lune.audio.coreaudio import (
    CoreAudioStreamOwner,
    StreamOwnerHealth,
    StreamOwnerStatus,
)
from lune.audio.devices import DeviceSnapshot
from lune.audio.silero import SileroVoiceDetector
from lune.audio.transport import LocalAudioTransport
from lune.audio.types import AudioSpan
from lune.config import AudioConfig
from lune.engine import EngineDependencies, VoiceEngine, compose_voice_engine
from lune.llm.budget import BudgetLedger
from lune.llm.contracts import (
    GenerationLLMTextFrame,
    ModelName,
    ProviderTerminalFrame,
)
from lune.llm.streaming import ScriptedAttemptProvider, StreamFrameFactory
from lune.memory.embedding import E5MemoryRetriever, LocalE5Encoder
from lune.memory.store import MemoryStore
from lune.paths import LunePaths
from lune.pipeline.factory import STTEventSink
from lune.pipeline.playback import DEFAULT_CAPACITY
from lune.pipeline.session import FinalOnlySTT
from lune.stt.contracts import FinalTranscript, STTEvent, TranscriptionRequest
from lune.stt.mlx import DECODING_TEMPERATURES, InferenceFunction, build_mlx_stt
from lune.tts.avspeech import AVSpeechAdapter
from lune.tts.contracts import PCMChunk
from lune.tts.router import TTSRouterService

type Scenario = Literal["preflight", "turn", "barge-in", "device-switch"]

_NORMAL_RESPONSE = "實體語音測試成功。"
# Three sentences, not one long one: `_speak` drains between sentences, so each
# one stays inside the bounded playback queue while the whole reply still plays
# long enough to interrupt. A single sentence beyond roughly six seconds of
# speech would overflow the queue instead of being interrupted.
_INTERRUPT_RESPONSE = "我先說一段比較長的話。你可以隨時打斷我。現在請開口說話。"


@dataclass(slots=True)
class SanitizedDetectorProbe:
    """Retain only aggregate acoustic evidence, never PCM or transcript text."""

    detector: SileroVoiceDetector
    windows: int = 0
    voiced_windows: int = 0
    max_confidence: float = 0.0
    max_abs_sample: int = 0
    anchor: float | None = None
    max_lag_ms: float = 0.0
    last_lag_ms: float = 0.0

    @property
    def frames_required(self) -> int:
        return self.detector.frames_required

    def is_voiced(self, span: AudioSpan) -> bool:
        if self.anchor is not None:
            # How far the pipeline trails the microphone. `speech_end_at` is
            # derived from processing time, so this lag is exactly the amount by
            # which the end-to-end figure flatters itself.
            elapsed_ms = (time.monotonic() - self.anchor) * 1_000.0
            audio_ms = span.end_sample * 1_000.0 / span.sample_rate
            self.last_lag_ms = round(elapsed_ms - audio_ms, 3)
            self.max_lag_ms = max(self.max_lag_ms, self.last_lag_ms)
        confidence = self.detector.voice_confidence(span)
        samples = np.frombuffer(span.pcm, dtype="<i2")
        peak = int(np.abs(samples.astype(np.int32)).max()) if samples.size else 0
        self.windows += 1
        self.max_confidence = max(self.max_confidence, confidence)
        self.max_abs_sample = max(self.max_abs_sample, peak)
        voiced = confidence >= self.detector.confidence_threshold
        if voiced:
            self.voiced_windows += 1
        return voiced


@dataclass(slots=True)
class SanitizedSTTProbe:
    """Time recognition without retaining any transcript text.

    Only durations survive here: the audio length that was submitted and how
    long inference took. Neither is transcript content, and both are needed to
    tell a slow model apart from a microphone that captured nothing.
    """

    finals: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    submitted: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    audio_ms: list[float] = field(default_factory=list)
    temperatures: list[float] = field(default_factory=list)
    avg_logprobs: list[float] = field(default_factory=list)
    decoded_chars: list[int] = field(default_factory=list)
    _started: dict[str, float] = field(default_factory=dict)

    def record_decoding(self, temperatures: list[float], logprobs: list[float], chars: int) -> None:
        self.temperatures.extend(round(value, 3) for value in temperatures)
        self.avg_logprobs.extend(round(value, 3) for value in logprobs)
        self.decoded_chars.append(chars)

    def submit(self, request: TranscriptionRequest) -> None:
        self.submitted += 1
        self._started[request.request_id] = time.monotonic()
        self.audio_ms.append(
            round(request.audio.frame_count * 1_000.0 / request.audio.sample_rate, 3)
        )

    def record(self, event: STTEvent) -> None:
        started = self._started.pop(event.request_id, None)
        if started is not None:
            self.latencies_ms.append(round((time.monotonic() - started) * 1_000.0, 3))
        if isinstance(event, FinalTranscript):
            self.finals += 1
            return
        self.failures[event.code] = self.failures.get(event.code, 0) + 1


@dataclass(slots=True)
class ObservedSTT:
    """Wrap the real service so submissions can be timed at the fence."""

    inner: FinalOnlySTT
    probe: SanitizedSTTProbe

    def set_generation(self, generation_id: int) -> None:
        self.inner.set_generation(generation_id)

    def submit(self, request: TranscriptionRequest) -> bool:
        accepted = self.inner.submit(request)
        if accepted:
            self.probe.submit(request)
        return accepted

    async def close(self) -> None:
        await self.inner.close()


@dataclass(slots=True)
class ObservedStreams:
    """Delegate to the real owner while counting which stream actually failed."""

    inner: CoreAudioStreamOwner
    input_failures: int = 0
    output_failures: int = 0

    async def default_devices(self) -> DeviceSnapshot:
        return await self.inner.default_devices()

    async def rebuild_streams(self, snapshot: DeviceSnapshot) -> None:
        await self.inner.rebuild_streams(snapshot)

    async def set_microphone(self, enabled: bool) -> None:
        await self.inner.set_microphone(enabled)

    async def request_microphone_access(self) -> None:
        await self.inner.request_microphone_access()

    async def write(self, chunk: PCMChunk) -> None:
        await self.inner.write(chunk)

    async def flush(self) -> None:
        await self.inner.flush()

    async def close(self) -> None:
        await self.inner.close()

    def consume_health(self) -> StreamOwnerHealth:
        health = self.inner.consume_health()
        self.input_failures += int(health.input_failed)
        self.output_failures += int(health.output_failed)
        return health

    def status(self) -> StreamOwnerStatus:
        return self.inner.status()


async def _warm_stt(manifest_path: Path) -> tuple[str, float]:
    """Load and compile Whisper before the microphone opens.

    The 10 s STT watchdog is a product threshold, so the first real utterance
    must not also pay for weight loading and MLX graph compilation.
    """

    outcome: list[str] = []

    async def capture(event: STTEvent) -> None:
        outcome.append("final" if isinstance(event, FinalTranscript) else event.code)

    frames = 16_000
    request = TranscriptionRequest(
        request_id="warm-up",
        generation_id=0,
        audio=AudioSpan(
            pcm=bytes(frames * 2),
            start_sample=0,
            end_sample=frames,
            generation_id=0,
        ),
    )
    service = build_mlx_stt(manifest_path=manifest_path, generation_id=0, emit=capture)
    started = time.monotonic()
    try:
        service.submit(request)
        await _wait_until(lambda: bool(outcome), 300.0)
    finally:
        await service.close()
    return outcome[0], round((time.monotonic() - started) * 1_000.0, 3)


def _text(value: str) -> StreamFrameFactory:
    return lambda generation, attempt: GenerationLLMTextFrame(
        text=value,
        generation_id=generation,
        attempt_id=attempt,
    )


def _terminal() -> StreamFrameFactory:
    return lambda generation, attempt: ProviderTerminalFrame(
        generation_id=generation,
        attempt_id=attempt,
        status="completed",
    )


def _observed_inference(probe: SanitizedSTTProbe) -> InferenceFunction:
    """Mirror `lune.stt.mlx._default_inference`, recording only decode numbers.

    Temperature and average log-probability say whether Whisper fell back to a
    second and third decoding pass, which is the difference between a slow
    machine and a recording the model finds hard. Neither is transcript text.
    """

    def inference(request: TranscriptionRequest, model_root: Path) -> str:
        module = importlib.import_module("mlx_whisper")
        audio = np.frombuffer(request.audio.pcm, dtype="<i2").astype(np.float32) / 32768.0
        raw = module.transcribe(
            audio,
            path_or_hf_repo=str(model_root),
            verbose=None,
            temperature=DECODING_TEMPERATURES,
        )
        segments = raw.get("segments", [])
        text = str(raw["text"]).strip()
        probe.record_decoding(
            [float(item.get("temperature", -1.0)) for item in segments],
            [float(item.get("avg_logprob", 0.0)) for item in segments],
            len(text),
        )
        return text

    return inference


def _stt_fields(probe: SanitizedSTTProbe | None) -> dict[str, object]:
    if probe is None:
        return {}
    return {
        "stt_submitted": probe.submitted,
        "stt_finals": probe.finals,
        "stt_failures": probe.failures,
        "stt_latency_ms": probe.latencies_ms,
        "stt_audio_ms": probe.audio_ms,
        "stt_temperatures": probe.temperatures,
        "stt_avg_logprobs": probe.avg_logprobs,
        "stt_decoded_chars": probe.decoded_chars,
    }


def _cancel_reasons(engine: VoiceEngine) -> dict[str, int]:
    counts: dict[str, int] = {}
    for cancel in engine.pipeline.coordinator.cancel_events:
        counts[cancel.reason] = counts.get(cancel.reason, 0) + 1
    return counts


def _emit(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True, sort_keys=True), flush=True)


async def _wait_until(predicate: Callable[[], bool], timeout_s: float) -> None:
    async with asyncio.timeout(timeout_s):
        # Device callbacks and pipeline reports have no shared notification hook.
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


def _provider(response: str) -> Mapping[ModelName, ScriptedAttemptProvider]:
    # A physical run needs as many attempts as the speaker takes, so the one
    # scripted answer has to survive every retry instead of being consumed once.
    primary = ScriptedAttemptProvider(
        "gpt-5.6-terra",
        scripts=((_text(response), _terminal()),),
        repeat_last=True,
    )
    fallback = ScriptedAttemptProvider("gpt-5.6-luna", scripts=())
    return {"gpt-5.6-terra": primary, "gpt-5.6-luna": fallback}


async def _build_engine(
    scenario: Scenario,
    stt_timeout_s: float,
) -> tuple[VoiceEngine, ObservedStreams, SanitizedDetectorProbe, SanitizedSTTProbe]:
    paths = LunePaths.defaults()
    audio = AudioConfig()
    store = MemoryStore.ephemeral()
    session_id = store.start_session("physical-smoke")
    encoder = LocalE5Encoder(paths.e5_manifest)
    started = time.monotonic()
    await asyncio.to_thread(encoder.encode_query, "local readiness probe")
    e5_warm_ms = round((time.monotonic() - started) * 1_000.0, 3)
    stt_outcome, stt_warm_ms = await _warm_stt(paths.whisper_manifest)
    _emit(
        "warm_up",
        e5_ms=e5_warm_ms,
        whisper_ms=stt_warm_ms,
        whisper_outcome=stt_outcome,
    )
    retriever = E5MemoryRetriever(store, encoder)
    transport = LocalAudioTransport(
        sample_rate=audio.sample_rate,
        channels=audio.channels,
    )
    streams = ObservedStreams(CoreAudioStreamOwner(transport))
    detector = SanitizedDetectorProbe(SileroVoiceDetector(sample_rate=audio.sample_rate))

    stt_probe = SanitizedSTTProbe()

    def stt_factory(emit: STTEventSink) -> FinalOnlySTT:
        async def observed(event: STTEvent) -> None:
            stt_probe.record(event)
            await emit(event)

        return ObservedSTT(
            inner=build_mlx_stt(
                manifest_path=paths.whisper_manifest,
                generation_id=0,
                emit=observed,
                inference=_observed_inference(stt_probe),
            ),
            probe=stt_probe,
        )

    response = _NORMAL_RESPONSE if scenario == "turn" else _INTERRUPT_RESPONSE
    dependencies = EngineDependencies(
        session_id=session_id,
        store=store,
        retriever=retriever,
        detector=detector,
        stt_factory=stt_factory,
        providers=_provider(response),
        ledger=BudgetLedger(),
        tts=TTSRouterService(avspeech=AVSpeechAdapter()),
        audio=audio,
    )
    return (
        compose_voice_engine(
            dependencies,
            transport=transport,
            streams=streams,
            stt_timeout_s=stt_timeout_s,
        ),
        streams,
        detector,
        stt_probe,
    )


async def _run(scenario: Scenario, timeout_s: float, stt_timeout_s: float) -> int:
    engine: VoiceEngine | None = None
    streams: ObservedStreams | None = None
    detector: SanitizedDetectorProbe | None = None
    stt: SanitizedSTTProbe | None = None
    try:
        engine, streams, detector, stt = await _build_engine(scenario, stt_timeout_s)
        state = await engine.start()
        _emit(
            "cold_start",
            state=state,
            microphone_enabled=engine.transport.microphone_enabled,
            output_is_builtin=state == "paused_unsafe_output",
            queue_capacity=DEFAULT_CAPACITY,
            stt_timeout_s=stt_timeout_s,
        )
        if state == "paused_unsafe_output":
            _emit("blocked", reason="safe_physical_output_required")
            return 4
        if state != "mic_off":
            _emit("error", code="unexpected_cold_start_state")
            return 5
        if scenario == "preflight":
            return 0

        state = await engine.set_microphone(True)
        owner_status = streams.status()
        detector.anchor = time.monotonic()
        _emit(
            "listening",
            state=state,
            microphone_enabled=engine.transport.microphone_enabled,
            input_open=owner_status.input_open,
            input_sample_rate=owner_status.input_sample_rate,
            input_channels=owner_status.input_channels,
        )
        if state != "listening" or not owner_status.input_open:
            _emit("error", code="microphone_did_not_open")
            return 6

        if scenario == "turn":
            _emit("action", name="speak_one_utterance")
            # A barge-in cancels its turn by design, so keep listening for a
            # completed one instead of scoring the interrupted attempt.
            reported = 0

            def completed() -> bool:
                nonlocal reported
                attempts = engine.pipeline.session.reports
                for attempt in attempts[reported:]:
                    _emit(
                        "turn_attempt",
                        outcome=attempt.outcome,
                        sentences_played=attempt.sentences_played,
                        degraded_tts=attempt.degraded_tts,
                    )
                reported = len(attempts)
                return any(attempt.outcome == "completed" for attempt in attempts)

            await _wait_until(completed, timeout_s)
            report = next(
                candidate
                for candidate in reversed(engine.pipeline.session.reports)
                if candidate.outcome == "completed"
            )
            speech_end = engine.pipeline.session.speech_end_at(report.generation_id)
            first_audible = engine.pipeline.playback.first_audible_at(report.generation_id)
            latency_ms = (
                None
                if speech_end is None or first_audible is None
                else round((first_audible - speech_end) * 1_000.0, 3)
            )
            owner_status = streams.status()
            _emit(
                "turn_result",
                outcome=report.outcome,
                sentences_played=report.sentences_played,
                degraded_tts=report.degraded_tts,
                speech_end_to_first_audio_ms=latency_ms,
                output_open=owner_status.output_open,
                output_sample_rate=owner_status.output_sample_rate,
                output_channels=owner_status.output_channels,
                transport_overflowed=engine.transport.health().overflowed,
                playback_overflowed=engine.pipeline.playback.health().overflowed,
                cancel_reasons=_cancel_reasons(engine),
                input_lag_ms=detector.last_lag_ms,
                max_input_lag_ms=detector.max_lag_ms,
                input_stream_failures=streams.input_failures,
                output_stream_failures=streams.output_failures,
                turn_outcomes=[item.outcome for item in engine.pipeline.session.reports],
                **_stt_fields(stt),
            )
            return 0 if report.outcome == "completed" and latency_ms is not None else 7

        _emit(
            "action",
            name="interrupt_when_audio_starts"
            if scenario == "barge-in"
            else "switch_or_disconnect_output_when_audio_starts",
        )
        reason = "barge_in" if scenario == "barge-in" else "device_changed"
        await _wait_until(
            lambda: any(
                event.reason == reason for event in engine.pipeline.coordinator.cancel_events
            ),
            timeout_s,
        )
        event = next(
            event
            for event in reversed(engine.pipeline.coordinator.cancel_events)
            if event.reason == reason
        )
        if scenario == "barge-in":
            await engine.set_microphone(False)
        await engine.pipeline.session.wait_for_turns(timeout_s=2.0)
        _emit(
            "cancellation_result",
            reason=reason,
            audible_stop_ms=round(event.audible_stop_ms, 3),
            under_200_ms=event.audible_stop_ms <= 200.0,
            failed_stages=list(event.failed_stages),
            old_generation_stopped=engine.pipeline.playback.is_stopped(
                event.previous_generation_id
            ),
            complete_turns=len(engine.pipeline.session.reports),
            state=engine.state,
        )
        return 0 if event.audible_stop_ms <= 200.0 and not event.failed_stages else 8
    except TimeoutError:
        cancel_reasons = {} if engine is None else _cancel_reasons(engine)
        _emit(
            "error",
            code="scenario_timeout",
            state=None if engine is None else engine.state,
            vad_windows=0 if detector is None else detector.windows,
            voiced_windows=0 if detector is None else detector.voiced_windows,
            max_vad_confidence=(None if detector is None else round(detector.max_confidence, 6)),
            max_abs_sample=None if detector is None else detector.max_abs_sample,
            transport_overflowed=(None if engine is None else engine.transport.health().overflowed),
            cancel_reasons=cancel_reasons,
            input_stream_failures=None if streams is None else streams.input_failures,
            output_stream_failures=None if streams is None else streams.output_failures,
            complete_turns=None if engine is None else len(engine.pipeline.session.reports),
            **_stt_fields(stt),
        )
        return 9
    except Exception as error:
        _emit("error", code="scenario_failed", error_type=type(error).__name__)
        return 10
    finally:
        if engine is not None:
            await engine.close()
            await asyncio.sleep(0)
        if streams is not None:
            status = streams.status()
            residual_tasks = sorted(
                task.get_name()
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and not task.done()
                and task.get_name().startswith("lune-")
            )
            _emit(
                "shutdown",
                input_stream_failures=streams.input_failures,
                output_stream_failures=streams.output_failures,
                engine_tasks=0 if engine is None else engine.background_task_count,
                input_open=status.input_open,
                output_open=status.output_open,
                host_active=status.host_active,
                child_processes=len(multiprocessing.active_children()),
                residual_lune_tasks=residual_tasks,
            )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "scenario",
        choices=("preflight", "turn", "barge-in", "device-switch"),
    )
    parser.add_argument("--timeout-s", type=float, default=60.0)
    # The engine default is the product watchdog. Measured Whisper latency on
    # this hardware can exceed it, so a run may raise it deliberately — the
    # value is emitted with the evidence rather than changed in the engine.
    parser.add_argument("--stt-timeout-s", type=float, default=10.0)
    args = parser.parse_args(argv)
    if args.timeout_s <= 0:
        parser.error("--timeout-s must be positive")
    if args.stt_timeout_s <= 0:
        parser.error("--stt-timeout-s must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args.scenario, args.timeout_s, args.stt_timeout_s))


if __name__ == "__main__":
    raise SystemExit(main())
