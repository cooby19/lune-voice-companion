from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path

from lune.tts_spike.sandbox import ProbeProcessResult, probe_sandbox


def _executable(path: Path) -> Path:
    path.write_text("fixture", encoding="utf-8")
    path.chmod(0o700)
    return path


def test_active_file_and_network_denials_are_required(tmp_path: Path) -> None:
    sandbox = _executable(tmp_path / "sandbox-exec")
    python = _executable(tmp_path / "python")
    canary = tmp_path / "canary"
    canary.write_text("private", encoding="utf-8")
    seen_environment: Mapping[str, str] | None = None

    def runner(
        argv: tuple[str, ...], environment: Mapping[str, str], timeout: float
    ) -> ProbeProcessResult:
        nonlocal seen_environment
        assert argv[0] == str(sandbox)
        assert "(deny network*)" in argv[2]
        assert timeout == 5.0
        seen_environment = environment
        return ProbeProcessResult(
            returncode=0,
            stdout=json.dumps({"file_denied": True, "network_denied": True}),
        )

    check = probe_sandbox(
        canary_path=canary,
        sandbox_executable=sandbox,
        python_executable=python,
        platform_name="darwin",
        runner=runner,
    )
    assert check.available
    assert seen_environment is not None
    assert "OPENAI_API_KEY" not in seen_environment
    assert "SSH_AUTH_SOCK" not in seen_environment


def test_probe_fails_closed_if_a_denial_does_not_apply(tmp_path: Path) -> None:
    sandbox = _executable(tmp_path / "sandbox-exec")
    python = _executable(tmp_path / "python")
    canary = tmp_path / "canary"
    canary.write_text("private", encoding="utf-8")

    def runner(
        _argv: tuple[str, ...], _environment: Mapping[str, str], _timeout: float
    ) -> ProbeProcessResult:
        return ProbeProcessResult(
            returncode=4,
            stdout=json.dumps({"file_denied": False, "network_denied": True}),
        )

    check = probe_sandbox(
        canary_path=canary,
        sandbox_executable=sandbox,
        python_executable=python,
        platform_name="darwin",
        runner=runner,
    )
    assert not check.available
    assert check.reason == "probe_denial_failed"


def test_sandbox_exec_rejection_is_opaque(tmp_path: Path) -> None:
    sandbox = _executable(tmp_path / "sandbox-exec")
    python = _executable(tmp_path / "python")
    canary = tmp_path / "canary"
    canary.write_text("private", encoding="utf-8")

    def runner(
        _argv: tuple[str, ...], _environment: Mapping[str, str], _timeout: float
    ) -> ProbeProcessResult:
        return ProbeProcessResult(returncode=71, stdout="")

    check = probe_sandbox(
        canary_path=canary,
        sandbox_executable=sandbox,
        python_executable=python,
        platform_name="darwin",
        runner=runner,
    )
    assert check.reason == "probe_rejected"
    assert str(canary) not in repr(check)


def test_probe_timeout_fails_closed(tmp_path: Path) -> None:
    sandbox = _executable(tmp_path / "sandbox-exec")
    python = _executable(tmp_path / "python")
    canary = tmp_path / "canary"
    canary.write_text("private", encoding="utf-8")

    def runner(
        argv: tuple[str, ...], _environment: Mapping[str, str], timeout: float
    ) -> ProbeProcessResult:
        raise subprocess.TimeoutExpired(argv, timeout)

    check = probe_sandbox(
        canary_path=canary,
        sandbox_executable=sandbox,
        python_executable=python,
        platform_name="darwin",
        runner=runner,
    )
    assert check.reason == "probe_timeout"


def test_non_macos_never_invokes_runner(tmp_path: Path) -> None:
    called = False

    def runner(
        _argv: tuple[str, ...], _environment: Mapping[str, str], _timeout: float
    ) -> ProbeProcessResult:
        nonlocal called
        called = True
        return ProbeProcessResult(returncode=0, stdout="{}")

    check = probe_sandbox(
        canary_path=tmp_path / "absent",
        platform_name="linux",
        runner=runner,
    )
    assert check.reason == "unsupported_platform"
    assert not called
