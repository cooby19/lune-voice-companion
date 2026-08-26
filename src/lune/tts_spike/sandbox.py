"""Active, fail-closed capability probe for the deprecated macOS seatbelt tool."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

SandboxReason = Literal[
    "available",
    "not_probed",
    "unsupported_platform",
    "sandbox_exec_missing",
    "sandbox_exec_not_executable",
    "probe_canary_missing",
    "probe_python_unavailable",
    "probe_timeout",
    "probe_launch_failed",
    "probe_rejected",
    "probe_output_invalid",
    "probe_denial_failed",
]

_PROBE_PROGRAM: Final[str] = """
import errno
import json
import socket
import sys

denied_errors = {errno.EACCES, errno.EPERM}
try:
    with open(sys.argv[1], "rb") as handle:
        handle.read(1)
except OSError as error:
    file_denied = error.errno in denied_errors
else:
    file_denied = False

network_socket = None
try:
    network_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    network_socket.bind(("127.0.0.1", 0))
except OSError as error:
    network_denied = error.errno in denied_errors
else:
    network_denied = False
finally:
    if network_socket is not None:
        network_socket.close()

print(json.dumps({"file_denied": file_denied, "network_denied": network_denied}))
raise SystemExit(0 if file_denied and network_denied else 4)
""".strip()


@dataclass(frozen=True, slots=True)
class ProbeProcessResult:
    returncode: int
    stdout: str


ProbeRunner = Callable[[tuple[str, ...], Mapping[str, str], float], ProbeProcessResult]


@dataclass(frozen=True, slots=True)
class SandboxCheck:
    reason: SandboxReason
    executable: Path | None = field(default=None, repr=False)

    @property
    def available(self) -> bool:
        return self.reason == "available" and self.executable is not None


def _default_runner(
    argv: tuple[str, ...], env: Mapping[str, str], timeout_seconds: float
) -> ProbeProcessResult:
    completed = subprocess.run(  # noqa: S603 - fixed executable and no shell are deliberate.
        argv,
        check=False,
        capture_output=True,
        env=dict(env),
        text=True,
        timeout=timeout_seconds,
    )
    return ProbeProcessResult(returncode=completed.returncode, stdout=completed.stdout)


def _sbpl_literal(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _probe_profile(canary_path: Path) -> str:
    literal = _sbpl_literal(str(canary_path))
    return "\n".join(
        (
            "(version 1)",
            "(allow default)",
            f"(deny file-read-data (literal {literal}))",
            "(deny network*)",
        )
    )


def probe_sandbox(
    *,
    canary_path: Path,
    sandbox_executable: Path = Path("/usr/bin/sandbox-exec"),
    python_executable: Path | None = None,
    platform_name: str | None = None,
    timeout_seconds: float = 5.0,
    runner: ProbeRunner = _default_runner,
) -> SandboxCheck:
    """Require proof that both file and network denials are actively enforced."""

    current_platform = sys.platform if platform_name is None else platform_name
    if current_platform != "darwin":
        return SandboxCheck(reason="unsupported_platform")
    if not sandbox_executable.is_file():
        return SandboxCheck(reason="sandbox_exec_missing")
    if not os.access(sandbox_executable, os.X_OK):
        return SandboxCheck(reason="sandbox_exec_not_executable")
    if not canary_path.is_file():
        return SandboxCheck(reason="probe_canary_missing")

    selected_python = Path(sys.executable) if python_executable is None else python_executable
    if not selected_python.is_file() or not os.access(selected_python, os.X_OK):
        return SandboxCheck(reason="probe_python_unavailable")

    argv = (
        str(sandbox_executable),
        "-p",
        _probe_profile(canary_path),
        str(selected_python),
        "-I",
        "-c",
        _PROBE_PROGRAM,
        str(canary_path),
    )
    environment = {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    try:
        result = runner(argv, environment, timeout_seconds)
    except subprocess.TimeoutExpired:
        return SandboxCheck(reason="probe_timeout")
    except OSError:
        return SandboxCheck(reason="probe_launch_failed")

    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        if result.returncode != 0:
            return SandboxCheck(reason="probe_rejected")
        return SandboxCheck(reason="probe_output_invalid")
    if not isinstance(payload, dict) or set(payload) != {"file_denied", "network_denied"}:
        return SandboxCheck(reason="probe_output_invalid")
    if payload.get("file_denied") is not True or payload.get("network_denied") is not True:
        return SandboxCheck(reason="probe_denial_failed")
    if result.returncode != 0:
        return SandboxCheck(reason="probe_rejected")
    return SandboxCheck(reason="available", executable=sandbox_executable)
