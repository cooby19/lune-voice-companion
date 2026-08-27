"""Session-scoped circuit breaker for the optional GPT-SoVITS backend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type CircuitState = Literal["closed", "open"]


@dataclass(slots=True)
class TTSCircuitBreaker:
    failure_threshold: int = 2
    _consecutive_failures: int = 0
    _state: CircuitState = "closed"

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure threshold must be positive")

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def allows_request(self) -> bool:
        return self._state == "closed"

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def record_success(self) -> None:
        if self._state == "closed":
            self._consecutive_failures = 0

    def record_failure(self) -> None:
        if self._state == "open":
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._state = "open"

    def record_rebuild_failure(self) -> None:
        self._consecutive_failures = max(self.failure_threshold, self._consecutive_failures + 1)
        self._state = "open"
