"""Construct the release-safe TTS router from public configuration."""

from __future__ import annotations

from pathlib import Path

from lune.config import TTSConfig
from lune.paths import LunePaths
from lune.tts.avspeech import AVSpeechAdapter
from lune.tts.gpt_sovits import GPTSoVITSBackend, SandboxedGPTWorkerLauncher
from lune.tts.router import BackendName, TTSRouterService


def build_tts_router(
    config: TTSConfig,
    paths: LunePaths,
    *,
    gpt_performance_gate_passed: bool = False,
) -> TTSRouterService:
    """Keep AVSpeech default until an authorized private performance gate passes."""

    avspeech = AVSpeechAdapter()
    gpt_backend = GPTSoVITSBackend(
        SandboxedGPTWorkerLauncher(
            python_executable=Path(config.gpt_worker_python),
            runtime_root=paths.gpt_sovits_runtime,
            manifest_path=paths.tts_manifest,
        )
    )
    preferred: BackendName = (
        "gpt_sovits"
        if config.preferred_backend == "gpt_sovits" and gpt_performance_gate_passed
        else "avspeech"
    )
    return TTSRouterService(
        avspeech=avspeech,
        gpt_sovits=gpt_backend,
        preferred_backend=preferred,
    )
