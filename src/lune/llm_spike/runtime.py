"""Neutral description of local runtime candidates and the loopback-only endpoint policy.

No candidate is a release dependency. Each entry records what adopting it would cost -
an install, an extra managed process, a network listener - so the choice stays an explicit
user decision rather than something the spike silently settles. `check_local_endpoint`
enforces the standing rule that a local model endpoint may only ever bind loopback.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal
from urllib.parse import urlsplit

type LocalRuntimeName = Literal[
    "mlx_lm_in_process",
    "mlx_lm_worker",
    "llama_cpp_server",
    "ollama",
]
type RuntimeStatus = Literal[
    "not_authorised",
    "authorised_not_installed",
    "installed",
    "unavailable",
]
type EndpointReason = Literal[
    "ok",
    "url_invalid",
    "scheme_not_http",
    "credentials_present",
    "host_missing",
    "host_not_loopback",
    "port_missing",
    "port_out_of_range",
    "query_present",
    "fragment_present",
]

_LOOPBACK_NAMES: Final[frozenset[str]] = frozenset({"localhost"})


@dataclass(frozen=True, slots=True)
class RuntimeCandidate:
    """What one runtime would cost if the user adopted it."""

    name: LocalRuntimeName
    requires_install: bool
    adds_managed_process: bool
    binds_network_listener: bool
    shares_engine_address_space: bool
    notes: str


CANDIDATES: Final[MappingProxyType[LocalRuntimeName, RuntimeCandidate]] = MappingProxyType(
    {
        "mlx_lm_in_process": RuntimeCandidate(
            name="mlx_lm_in_process",
            requires_install=True,
            adds_managed_process=False,
            binds_network_listener=False,
            shares_engine_address_space=True,
            notes=(
                "Mirrors the existing optional `mlx` extra used for Whisper. No extra PID and "
                "no listener, but model weights sit in the engine's own address space and a "
                "synchronous generate call must be fenced off the event loop."
            ),
        ),
        "mlx_lm_worker": RuntimeCandidate(
            name="mlx_lm_worker",
            requires_install=True,
            adds_managed_process=True,
            binds_network_listener=False,
            shares_engine_address_space=False,
            notes=(
                "Fourth managed process alongside UI, engine and the GPT-SoVITS worker. "
                "Isolates weights and makes hard cancellation possible by killing a verified "
                "PID, at the cost of the process budget the plan currently fixes at three."
            ),
        ),
        "llama_cpp_server": RuntimeCandidate(
            name="llama_cpp_server",
            requires_install=True,
            adds_managed_process=True,
            binds_network_listener=True,
            shares_engine_address_space=False,
            notes=(
                "GGUF Q4 via a loopback HTTP server. OpenAI-compatible routes are not the "
                "Responses WebSocket contract, so it needs its own provider rather than being "
                "presented as a drop-in replacement."
            ),
        ),
        "ollama": RuntimeCandidate(
            name="ollama",
            requires_install=True,
            adds_managed_process=True,
            binds_network_listener=True,
            shares_engine_address_space=False,
            notes=(
                "System-wide daemon outside Lune's lifecycle: it is not started, supervised or "
                "stopped by the engine, which conflicts with the quit-leaves-no-child rule and "
                "with pinning an artifact by revision and checksum."
            ),
        ),
    }
)


@dataclass(frozen=True, slots=True)
class RuntimeProbe:
    """Observed state of one candidate. Nothing is installed without authorisation."""

    name: LocalRuntimeName
    status: RuntimeStatus
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.status == "installed"


def unevaluated_probes() -> tuple[RuntimeProbe, ...]:
    """Every candidate before the user authorises any install."""

    return tuple(
        RuntimeProbe(name=name, status="not_authorised") for name in sorted(CANDIDATES.keys())
    )


@dataclass(frozen=True, slots=True)
class EndpointCheck:
    reason: EndpointReason

    @property
    def allowed(self) -> bool:
        return self.reason == "ok"


def check_local_endpoint(url: str) -> EndpointCheck:
    """Allow only a plain-HTTP loopback endpoint with an explicit port."""

    try:
        parts = urlsplit(url)
    except ValueError:
        return EndpointCheck("url_invalid")
    if parts.scheme != "http":
        return EndpointCheck("scheme_not_http")
    if parts.username is not None or parts.password is not None:
        return EndpointCheck("credentials_present")
    if parts.query:
        return EndpointCheck("query_present")
    if parts.fragment:
        return EndpointCheck("fragment_present")

    try:
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return EndpointCheck("url_invalid")
    if not hostname:
        return EndpointCheck("host_missing")
    if not _is_loopback(hostname):
        return EndpointCheck("host_not_loopback")
    if port is None:
        return EndpointCheck("port_missing")
    if not 1 <= port <= 65535:
        return EndpointCheck("port_out_of_range")
    return EndpointCheck("ok")


def _is_loopback(hostname: str) -> bool:
    if hostname in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False
