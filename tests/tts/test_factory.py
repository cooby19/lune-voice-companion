from pathlib import Path

from lune.config import TTSConfig
from lune.paths import LunePaths
from lune.tts.factory import build_tts_router


def test_factory_keeps_avspeech_default_until_private_gate_passes(tmp_path: Path) -> None:
    paths = LunePaths(tmp_path / "support", tmp_path / "logs")
    requested = TTSConfig(preferred_backend="gpt_sovits")

    safe_router = build_tts_router(requested, paths)
    eligible_router = build_tts_router(requested, paths, gpt_performance_gate_passed=True)

    assert safe_router.preferred_backend == "avspeech"
    assert eligible_router.preferred_backend == "gpt_sovits"
