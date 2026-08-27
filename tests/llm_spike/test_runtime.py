from __future__ import annotations

import pytest

from lune.llm_spike.runtime import CANDIDATES, check_local_endpoint, unevaluated_probes


def test_no_candidate_is_installed_before_authorisation() -> None:
    probes = unevaluated_probes()
    assert len(probes) == len(CANDIDATES)
    assert all(probe.status == "not_authorised" for probe in probes)
    assert not any(probe.usable for probe in probes)


def test_every_candidate_declares_its_architectural_cost() -> None:
    for candidate in CANDIDATES.values():
        assert candidate.requires_install
        assert candidate.notes.strip()
    assert CANDIDATES["mlx_lm_in_process"].adds_managed_process is False
    assert CANDIDATES["mlx_lm_in_process"].shares_engine_address_space is True
    assert CANDIDATES["ollama"].adds_managed_process is True
    assert CANDIDATES["ollama"].binds_network_listener is True


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:11434",
        "http://127.0.0.1:8080/v1/chat/completions",
        "http://localhost:1234",
        "http://[::1]:8000",
    ],
)
def test_loopback_endpoints_are_allowed(url: str) -> None:
    assert check_local_endpoint(url).allowed


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://127.0.0.1:8080", "scheme_not_http"),
        ("http://0.0.0.0:8080", "host_not_loopback"),
        ("http://192.168.1.10:8080", "host_not_loopback"),
        ("http://api.example.com:80", "host_not_loopback"),
        ("http://127.0.0.1", "port_missing"),
        ("http://user:pass@127.0.0.1:8080", "credentials_present"),
        ("http://127.0.0.1:8080?key=secret", "query_present"),
        ("http://127.0.0.1:8080#frag", "fragment_present"),
        ("ws://127.0.0.1:8080", "scheme_not_http"),
    ],
)
def test_non_loopback_or_unsafe_endpoints_are_refused(url: str, reason: str) -> None:
    check = check_local_endpoint(url)
    assert not check.allowed
    assert check.reason == reason


def test_out_of_range_port_is_refused() -> None:
    assert check_local_endpoint("http://127.0.0.1:99999").reason in {
        "port_out_of_range",
        "url_invalid",
    }
