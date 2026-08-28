"""Typed M3 contracts with generation and attempt correlation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pipecat.frames.frames import Frame, FunctionCallInProgressFrame, LLMTextFrame, SystemFrame

type ModelName = Literal["gpt-5.6-terra", "gpt-5.6-luna"]
type ProviderName = Literal["openai_responses", "deterministic_fake"]
type TerminalStatus = Literal["completed", "failed", "incomplete", "cancelled"]
type ProviderErrorCode = Literal[
    "busy",
    "rate_limited",
    "connection_lost",
    "provider_error",
    "stream_incomplete",
    "cancelled",
]
_PROVIDER_ERROR_CODES = frozenset(
    {
        "busy",
        "rate_limited",
        "connection_lost",
        "provider_error",
        "stream_incomplete",
        "cancelled",
    }
)


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    function_calling: bool
    remote_cancel: bool
    usage_reporting: bool


@dataclass(slots=True)
class GenerationLLMTextFrame(LLMTextFrame):
    """Pipecat text frame fenced by both generation and provider attempt."""

    text: str = field(repr=False)
    generation_id: int = 0
    attempt_id: str = ""

    def __post_init__(self) -> None:
        LLMTextFrame.__post_init__(self)  # type: ignore[no-untyped-call]
        _validate_correlation(self.generation_id, self.attempt_id)

    def __str__(self) -> str:
        # Pipecat's TextFrame.__str__ prints the payload, which would defeat the
        # field's repr=False in logs, assertion output and exception messages.
        return (
            f"{type(self).__name__}(generation_id={self.generation_id!r}, "
            f"attempt_id={self.attempt_id!r})"
        )


@dataclass(kw_only=True, slots=True, repr=False)
class GenerationFunctionCallFrame(FunctionCallInProgressFrame):
    """Pipecat function-call frame whose arguments stay out of repr output."""

    generation_id: int
    attempt_id: str

    def __post_init__(self) -> None:
        Frame.__post_init__(self)  # type: ignore[no-untyped-call]
        _validate_correlation(self.generation_id, self.attempt_id)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(function_name={self.function_name!r}, "
            f"tool_call_id={self.tool_call_id!r}, generation_id={self.generation_id!r}, "
            f"attempt_id={self.attempt_id!r})"
        )


@dataclass(slots=True)
class AttemptUsageFrame(SystemFrame):
    """Usage correlated locally instead of relying on MetricsFrame queue order."""

    generation_id: int
    attempt_id: str
    input_tokens: int
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        Frame.__post_init__(self)  # type: ignore[no-untyped-call]
        _validate_correlation(self.generation_id, self.attempt_id)
        values = (
            self.input_tokens,
            self.cached_input_tokens,
            self.cache_write_input_tokens,
            self.output_tokens,
        )
        if any(value < 0 for value in values):
            raise ValueError("usage token counts cannot be negative")
        if self.cached_input_tokens + self.cache_write_input_tokens > self.input_tokens:
            raise ValueError("input token details cannot exceed total input tokens")


@dataclass(slots=True)
class ProviderTerminalFrame(SystemFrame):
    generation_id: int
    attempt_id: str
    status: TerminalStatus
    transient: bool = False
    error_code: ProviderErrorCode | None = None

    def __post_init__(self) -> None:
        Frame.__post_init__(self)  # type: ignore[no-untyped-call]
        _validate_correlation(self.generation_id, self.attempt_id)
        if self.status == "completed" and (self.transient or self.error_code is not None):
            raise ValueError("completed attempts cannot carry an error")
        if self.error_code is not None and self.error_code not in _PROVIDER_ERROR_CODES:
            raise ValueError("provider terminal frame requires a finite error code")


type ProviderStreamFrame = (
    GenerationLLMTextFrame | GenerationFunctionCallFrame | AttemptUsageFrame | ProviderTerminalFrame
)


def _validate_correlation(generation_id: int, attempt_id: str) -> None:
    if generation_id < 0:
        raise ValueError("generation ID cannot be negative")
    if not attempt_id or len(attempt_id) > 128:
        raise ValueError("attempt ID must contain 1 to 128 characters")
