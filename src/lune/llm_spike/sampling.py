"""Read-only macOS resource sampling for the local LLM spike.

Every reader degrades to `unknown` or `None` rather than raising, because a missing
sample must show up as a gate failure rather than as a crash mid-run. Nothing here reads
prompts, transcripts or private paths; it only reports numbers about this machine.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Final

from lune.llm_spike.performance import MemoryPressure, ThermalState

_PRESSURE_BY_LEVEL: Final[dict[int, MemoryPressure]] = {1: "normal", 2: "warn", 4: "critical"}
_THERMAL_BY_STATE: Final[dict[int, ThermalState]] = {
    0: "nominal",
    1: "fair",
    2: "serious",
    3: "critical",
}
_TIMEOUT_SECONDS: Final[float] = 5.0


@dataclass(frozen=True, slots=True)
class ResourceSample:
    rss_bytes: int | None
    swap_used_bytes: int | None
    memory_pressure: MemoryPressure
    thermal_state: ThermalState


def _sysctl(name: str) -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["/usr/sbin/sysctl", "-n", name],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def read_memory_pressure() -> MemoryPressure:
    raw = _sysctl("kern.memorystatus_vm_pressure_level")
    if raw is None:
        return "unknown"
    try:
        return _PRESSURE_BY_LEVEL.get(int(raw), "unknown")
    except ValueError:
        return "unknown"


def read_swap_used_bytes() -> int | None:
    """Parse `used = NNN.NNM` out of `vm.swapusage`."""

    raw = _sysctl("vm.swapusage")
    if raw is None:
        return None
    fields = raw.replace("=", " = ").split()
    for index, field in enumerate(fields):
        if field == "used" and index + 2 < len(fields):
            return _parse_size(fields[index + 2])
    return None


def _parse_size(value: str) -> int | None:
    units = {"K": 1024, "M": 1024**2, "G": 1024**3}
    if not value or value[-1] not in units:
        return None
    try:
        return int(float(value[:-1]) * units[value[-1]])
    except ValueError:
        return None


def read_thermal_state() -> ThermalState:
    try:
        from Foundation import NSProcessInfo  # type: ignore[import-untyped]
    except ImportError:
        return "unknown"
    try:
        state = int(NSProcessInfo.processInfo().thermalState())
    except (AttributeError, ValueError):
        return "unknown"
    return _THERMAL_BY_STATE.get(state, "unknown")


def read_rss_bytes(pids: tuple[int, ...]) -> int | None:
    """Total resident set size across the given PIDs, in bytes."""

    if not pids:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["/bin/ps", "-o", "rss=", *[f"-p{pid}" for pid in pids]],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    total = 0
    seen = False
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if stripped.isdigit():
            total += int(stripped) * 1024
            seen = True
    return total if seen else None


def sample_resources(pids: tuple[int, ...]) -> ResourceSample:
    return ResourceSample(
        rss_bytes=read_rss_bytes(pids),
        swap_used_bytes=read_swap_used_bytes(),
        memory_pressure=read_memory_pressure(),
        thermal_state=read_thermal_state(),
    )
