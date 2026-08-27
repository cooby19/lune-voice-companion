from __future__ import annotations

import os
from pathlib import Path

from lune.llm_spike.sampling import _parse_size, sample_resources
from lune.llm_spike.worker import worker_environment, worker_script_path

FORBIDDEN = ("OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "HF_TOKEN")


def test_worker_environment_is_an_allowlist(tmp_path: Path) -> None:
    env = worker_environment(model_dir=tmp_path / "model", temp_root=tmp_path / "tmp")
    assert set(env) == {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "HF_HUB_DISABLE_TELEMETRY",
        "LUNE_QWEN_MODEL_DIR",
    }


def test_worker_environment_blocks_network_and_credentials(tmp_path: Path) -> None:
    env = worker_environment(model_dir=tmp_path / "model", temp_root=tmp_path / "tmp")
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["TRANSFORMERS_OFFLINE"] == "1"
    for name in FORBIDDEN:
        assert name not in env


def test_worker_environment_does_not_inherit_the_real_home(tmp_path: Path) -> None:
    env = worker_environment(model_dir=tmp_path / "model", temp_root=tmp_path / "tmp")
    assert env["HOME"] != os.path.expanduser("~")
    assert env["HOME"] == str(tmp_path / "tmp")


def test_worker_script_ships_with_the_package() -> None:
    script = worker_script_path()
    assert script.is_file()
    assert script.name == "qwen_worker.py"


def test_worker_script_imports_no_lune_module() -> None:
    source = worker_script_path().read_text(encoding="utf-8")
    assert "import lune" not in source
    assert "from lune" not in source


def test_parse_size_handles_macos_units() -> None:
    assert _parse_size("199.38M") == int(199.38 * 1024**2)
    assert _parse_size("1.00G") == 1024**3
    assert _parse_size("512K") == 512 * 1024
    assert _parse_size("nonsense") is None
    assert _parse_size("") is None


def test_sample_resources_returns_typed_values() -> None:
    sample = sample_resources((os.getpid(),))
    assert sample.memory_pressure in {"normal", "warn", "critical", "unknown"}
    assert sample.thermal_state in {"nominal", "fair", "serious", "critical", "unknown"}
    assert sample.rss_bytes is None or sample.rss_bytes > 0


def test_sample_resources_without_pids_reports_missing_rss() -> None:
    assert sample_resources(()).rss_bytes is None
