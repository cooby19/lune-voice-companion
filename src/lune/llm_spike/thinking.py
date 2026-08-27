"""Fail-closed removal of Qwen thinking content from a streaming response.

`Qwen3.5-4B` runs in thinking mode by default and marks reasoning with
``<think>``/``</think>``. The voice path requires non-thinking replies, and reasoning
must never reach `SentenceGate`, TTS, memory, SQLite or diagnostics. Disabling thinking
through the chat template is necessary but not sufficient: the spike must observe whether
the switch actually holds, so this filter both strips reasoning and records a violation
whenever any appears.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

from lune.llm_spike.tagscan import held_suffix

THINK_OPEN: Final[str] = "<think>"
THINK_CLOSE: Final[str] = "</think>"
_TAGS: Final[tuple[str, ...]] = (THINK_OPEN, THINK_CLOSE)
MAX_REASONING_CHARS: Final[int] = 32 * 1024

type ThinkingViolation = Literal[
    "reasoning_emitted",
    "unopened_reasoning_close",
    "unterminated_reasoning",
    "reasoning_overflow",
]


@dataclass(frozen=True, slots=True)
class ThinkingFilterResult:
    """Visible text plus every violation observed so far in this response."""

    text: str = field(repr=False)
    violations: tuple[ThinkingViolation, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.violations


class ThinkingFilter:
    """Strip reasoning from one response stream without ever buffering it downstream.

    Tags may be split across chunks, so a bounded suffix that could still become a tag is
    held back instead of being released. Reasoning bodies are counted and discarded as they
    arrive; they are never accumulated, so a runaway thinking block cannot grow memory or
    leak through an exception repr.
    """

    __slots__ = ("_closed", "_inside", "_pending", "_reasoning_chars", "_violations")

    def __init__(self) -> None:
        self._pending = ""
        self._inside = False
        self._reasoning_chars = 0
        self._violations: list[ThinkingViolation] = []
        self._closed = False

    @property
    def violations(self) -> tuple[ThinkingViolation, ...]:
        return tuple(self._violations)

    @property
    def clean(self) -> bool:
        return not self._violations

    def feed(self, text: str) -> ThinkingFilterResult:
        """Return the visible portion of ``text`` and the cumulative violation set."""

        if self._closed:
            raise ValueError("thinking filter is closed")
        self._pending += text
        visible: list[str] = []
        while True:
            if self._inside:
                if self._consume_reasoning():
                    continue
                break
            if self._consume_visible(visible):
                continue
            break
        return self._result("".join(visible))

    def finish(self) -> ThinkingFilterResult:
        """Flush a held-back partial tag; discard an unterminated reasoning block."""

        self._closed = True
        if self._inside:
            self._record("unterminated_reasoning")
            self._pending = ""
            return self._result("")
        tail, self._pending = self._pending, ""
        return self._result(tail)

    def reset(self) -> None:
        self._pending = ""
        self._inside = False
        self._reasoning_chars = 0
        self._violations.clear()
        self._closed = False

    def _consume_visible(self, visible: list[str]) -> bool:
        open_at = self._pending.find(THINK_OPEN)
        close_at = self._pending.find(THINK_CLOSE)
        if open_at >= 0 and (close_at < 0 or open_at < close_at):
            visible.append(self._pending[:open_at])
            self._pending = self._pending[open_at + len(THINK_OPEN) :]
            self._inside = True
            self._record("reasoning_emitted")
            return True
        if close_at >= 0:
            visible.append(self._pending[:close_at])
            self._pending = self._pending[close_at + len(THINK_CLOSE) :]
            self._record("unopened_reasoning_close")
            return True
        held = held_suffix(self._pending, _TAGS)
        split = len(self._pending) - held
        visible.append(self._pending[:split])
        self._pending = self._pending[split:]
        return False

    def _consume_reasoning(self) -> bool:
        close_at = self._pending.find(THINK_CLOSE)
        if close_at >= 0:
            self._count_reasoning(close_at)
            self._pending = self._pending[close_at + len(THINK_CLOSE) :]
            self._inside = False
            return True
        held = held_suffix(self._pending, _TAGS)
        split = len(self._pending) - held
        self._count_reasoning(split)
        self._pending = self._pending[split:]
        return False

    def _count_reasoning(self, size: int) -> None:
        self._reasoning_chars += size
        if self._reasoning_chars > MAX_REASONING_CHARS:
            self._record("reasoning_overflow")

    def _record(self, violation: ThinkingViolation) -> None:
        if violation not in self._violations:
            self._violations.append(violation)

    def _result(self, text: str) -> ThinkingFilterResult:
        return ThinkingFilterResult(text=text, violations=tuple(self._violations))


type ThinkingGateReason = ThinkingViolation | Literal["no_responses"]


@dataclass(frozen=True, slots=True)
class ThinkingGate:
    """Whether non-thinking mode actually held across a whole run."""

    evaluated: bool
    passed: bool
    reasons: tuple[ThinkingGateReason, ...]
    responses: int
    responses_with_reasoning: int


def evaluate_thinking(results: tuple[ThinkingFilterResult, ...]) -> ThinkingGate:
    """Fail if any response carried reasoning, however well the filter stripped it."""

    if not results:
        return ThinkingGate(
            evaluated=False,
            passed=False,
            reasons=("no_responses",),
            responses=0,
            responses_with_reasoning=0,
        )
    reasons: list[ThinkingGateReason] = []
    for result in results:
        for violation in result.violations:
            if violation not in reasons:
                reasons.append(violation)
    with_reasoning = sum(1 for result in results if result.violations)
    return ThinkingGate(
        evaluated=True,
        passed=not reasons,
        reasons=tuple(reasons),
        responses=len(results),
        responses_with_reasoning=with_reasoning,
    )
