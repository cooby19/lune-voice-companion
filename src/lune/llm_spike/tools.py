"""Host-side validation of local model tool calls, and the spike's tool-call gate.

The model may only ever propose. `propose_memory` and `propose_affinity` are checked here
against the same categories and limits the M4 proposal host commits with, so a local model
cannot widen what a cloud model was allowed to do. Arguments carry private content, so no
validator result embeds them in a repr.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from lune.llm_spike.performance import MIN_STABILITY_TURNS
from lune.llm_spike.tagscan import held_suffix
from lune.memory.proposals import MEMORY_CATEGORIES

PROPOSE_MEMORY: Final[str] = "propose_memory"
PROPOSE_AFFINITY: Final[str] = "propose_affinity"
ALLOWED_TOOLS: Final[frozenset[str]] = frozenset({PROPOSE_MEMORY, PROPOSE_AFFINITY})

MEMORY_TOOL: Final[dict[str, Any]] = {
    "type": "function",
    "function": {
        "name": PROPOSE_MEMORY,
        "description": (
            "Propose one durable fact worth remembering about the user. "
            "Only stable preferences, important people or events, explicit plans, "
            "or an explicit request to remember something."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "category": {"type": "string", "enum": sorted(MEMORY_CATEGORIES)},
                "importance": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": ["content", "category", "importance"],
        },
    },
}

AFFINITY_TOOL: Final[dict[str, Any]] = {
    "type": "function",
    "function": {
        "name": PROPOSE_AFFINITY,
        "description": "Propose a one-point change to the relationship score.",
        "parameters": {
            "type": "object",
            "properties": {
                "delta": {"type": "integer", "enum": [-1, 1]},
                "reason": {"type": "string"},
            },
            "required": ["delta", "reason"],
        },
    },
}
"""The two schemas the model is offered. Declared beside the validator that checks
their results, so a widened schema and an unchanged validator cannot drift apart."""

MAX_ARGUMENTS_BYTES: Final[int] = 4 * 1024
MAX_CONTENT_CHARS: Final[int] = 500
MAX_REASON_CHARS: Final[int] = 200
MAX_MEMORY_CALLS_PER_TURN: Final[int] = 1
MAX_AFFINITY_CALLS_PER_TURN: Final[int] = 1

_MEMORY_FIELDS: Final[frozenset[str]] = frozenset({"content", "category", "importance"})
_AFFINITY_FIELDS: Final[frozenset[str]] = frozenset({"delta", "reason"})

type ToolCallReason = Literal[
    "accepted",
    "unknown_tool",
    "arguments_too_large",
    "arguments_not_json",
    "arguments_not_object",
    "unexpected_field",
    "missing_field",
    "content_invalid",
    "category_invalid",
    "importance_invalid",
    "delta_invalid",
    "reason_invalid",
    "duplicate_content",
    "per_turn_limit_exceeded",
]
type ToolGateReason = Literal[
    "no_observations",
    "turns_insufficient",
    "invalid_call_accepted",
    "valid_call_rejected",
    "no_usable_tool_calls",
]


@dataclass(frozen=True, slots=True)
class ToolCallOutcome:
    tool_name: str
    reason: ToolCallReason

    @property
    def accepted(self) -> bool:
        return self.reason == "accepted"


@dataclass(frozen=True, slots=True)
class ToolCallObservation:
    """One tool call emitted by the model, with what the host is expected to do with it."""

    turn_index: int
    tool_name: str
    arguments_json: str = field(repr=False)
    expected_accept: bool = True

    def __post_init__(self) -> None:
        if self.turn_index < 0:
            raise ValueError("turn index cannot be negative")


class ToolCallValidator:
    """Validate calls for one session, enforcing per-turn limits and content dedupe."""

    __slots__ = ("_affinity_calls", "_memory_calls", "_seen_content", "_turn_index")

    def __init__(self) -> None:
        self._turn_index: int | None = None
        self._memory_calls = 0
        self._affinity_calls = 0
        self._seen_content: set[str] = set()

    def begin_turn(self, turn_index: int) -> None:
        if turn_index < 0:
            raise ValueError("turn index cannot be negative")
        self._turn_index = turn_index
        self._memory_calls = 0
        self._affinity_calls = 0

    def validate(self, tool_name: str, arguments_json: str) -> ToolCallOutcome:
        if tool_name not in ALLOWED_TOOLS:
            return ToolCallOutcome(tool_name, "unknown_tool")
        if len(arguments_json.encode("utf-8")) > MAX_ARGUMENTS_BYTES:
            return ToolCallOutcome(tool_name, "arguments_too_large")
        try:
            payload = json.loads(arguments_json)
        except (ValueError, RecursionError):
            return ToolCallOutcome(tool_name, "arguments_not_json")
        if not isinstance(payload, dict):
            return ToolCallOutcome(tool_name, "arguments_not_object")
        if tool_name == PROPOSE_MEMORY:
            return self._validate_memory(payload)
        return self._validate_affinity(payload)

    def _validate_memory(self, payload: dict[str, object]) -> ToolCallOutcome:
        field_check = _check_fields(payload, _MEMORY_FIELDS)
        if field_check is not None:
            return ToolCallOutcome(PROPOSE_MEMORY, field_check)
        if self._memory_calls >= MAX_MEMORY_CALLS_PER_TURN:
            return ToolCallOutcome(PROPOSE_MEMORY, "per_turn_limit_exceeded")

        content = payload["content"]
        if not isinstance(content, str) or not content.strip():
            return ToolCallOutcome(PROPOSE_MEMORY, "content_invalid")
        if len(content) > MAX_CONTENT_CHARS:
            return ToolCallOutcome(PROPOSE_MEMORY, "content_invalid")
        category = payload["category"]
        if not isinstance(category, str) or category not in MEMORY_CATEGORIES:
            return ToolCallOutcome(PROPOSE_MEMORY, "category_invalid")
        importance = payload["importance"]
        if isinstance(importance, bool) or not isinstance(importance, int | float):
            return ToolCallOutcome(PROPOSE_MEMORY, "importance_invalid")
        if not 0.0 <= float(importance) <= 1.0:
            return ToolCallOutcome(PROPOSE_MEMORY, "importance_invalid")

        normalized = normalize_content(content)
        if normalized in self._seen_content:
            return ToolCallOutcome(PROPOSE_MEMORY, "duplicate_content")
        self._seen_content.add(normalized)
        self._memory_calls += 1
        return ToolCallOutcome(PROPOSE_MEMORY, "accepted")

    def _validate_affinity(self, payload: dict[str, object]) -> ToolCallOutcome:
        field_check = _check_fields(payload, _AFFINITY_FIELDS)
        if field_check is not None:
            return ToolCallOutcome(PROPOSE_AFFINITY, field_check)
        if self._affinity_calls >= MAX_AFFINITY_CALLS_PER_TURN:
            return ToolCallOutcome(PROPOSE_AFFINITY, "per_turn_limit_exceeded")

        delta = payload["delta"]
        if isinstance(delta, bool) or not isinstance(delta, int) or delta not in {-1, 1}:
            return ToolCallOutcome(PROPOSE_AFFINITY, "delta_invalid")
        reason = payload["reason"]
        if not isinstance(reason, str) or not reason.strip():
            return ToolCallOutcome(PROPOSE_AFFINITY, "reason_invalid")
        if len(reason) > MAX_REASON_CHARS:
            return ToolCallOutcome(PROPOSE_AFFINITY, "reason_invalid")

        self._affinity_calls += 1
        return ToolCallOutcome(PROPOSE_AFFINITY, "accepted")


@dataclass(frozen=True, slots=True)
class ToolCallGate:
    evaluated: bool
    passed: bool
    reasons: tuple[ToolGateReason, ...]
    turns_observed: int
    accepted_calls: int
    rejected_calls: int
    malformed_ratio: float | None


def evaluate_tool_calls(
    observations: tuple[ToolCallObservation, ...],
    *,
    min_turns: int = MIN_STABILITY_TURNS,
) -> ToolCallGate:
    """Grade one run: the host must classify every call exactly as expected."""

    if not observations:
        return ToolCallGate(
            evaluated=False,
            passed=False,
            reasons=("no_observations",),
            turns_observed=0,
            accepted_calls=0,
            rejected_calls=0,
            malformed_ratio=None,
        )

    validator = ToolCallValidator()
    reasons: list[ToolGateReason] = []
    accepted = 0
    rejected = 0
    current_turn: int | None = None
    for observation in sorted(observations, key=lambda item: item.turn_index):
        if observation.turn_index != current_turn:
            validator.begin_turn(observation.turn_index)
            current_turn = observation.turn_index
        outcome = validator.validate(observation.tool_name, observation.arguments_json)
        if outcome.accepted:
            accepted += 1
        else:
            rejected += 1
        if outcome.accepted and not observation.expected_accept:
            _record(reasons, "invalid_call_accepted")
        elif not outcome.accepted and observation.expected_accept:
            _record(reasons, "valid_call_rejected")

    turns_observed = len({observation.turn_index for observation in observations})
    if turns_observed < min_turns:
        _record(reasons, "turns_insufficient")
    if accepted == 0:
        _record(reasons, "no_usable_tool_calls")

    total = accepted + rejected
    return ToolCallGate(
        evaluated=True,
        passed=not reasons,
        reasons=tuple(reasons),
        turns_observed=turns_observed,
        accepted_calls=accepted,
        rejected_calls=rejected,
        malformed_ratio=rejected / total if total else None,
    )


def normalize_content(content: str) -> str:
    """Fold a proposal to its dedupe key without keeping the original casing or spacing."""

    folded = unicodedata.normalize("NFKC", content).casefold()
    return " ".join(folded.split())


def _check_fields(payload: dict[str, object], expected: frozenset[str]) -> ToolCallReason | None:
    keys = set(payload.keys())
    if keys - expected:
        return "unexpected_field"
    if expected - keys:
        return "missing_field"
    return None


def _record(reasons: list[ToolGateReason], reason: ToolGateReason) -> None:
    if reason not in reasons:
        reasons.append(reason)


TOOL_CALL_OPEN: Final[str] = "<tool_call>"
TOOL_CALL_CLOSE: Final[str] = "</tool_call>"
_TOOL_TAGS: Final[tuple[str, ...]] = (TOOL_CALL_OPEN, TOOL_CALL_CLOSE)
MAX_TOOL_BLOCK_CHARS: Final[int] = 8 * 1024


@dataclass(frozen=True, slots=True)
class ExtractedToolCall:
    """One `<tool_call>` block lifted out of the text stream."""

    tool_name: str
    arguments_json: str = field(repr=False)
    malformed: bool = False


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    text: str = field(repr=False)
    tool_calls: tuple[ExtractedToolCall, ...] = ()


class ToolCallExtractor:
    """Pull Qwen `<tool_call>` blocks out of a stream so they never reach speech.

    Tags may straddle chunk boundaries, so a suffix that could still become a tag is
    withheld. A block that never closes, or that exceeds the size bound, is dropped and
    reported as malformed rather than leaking partial JSON downstream.
    """

    __slots__ = ("_block", "_closed", "_inside", "_pending")

    def __init__(self) -> None:
        self._pending = ""
        self._block = ""
        self._inside = False
        self._closed = False

    def feed(self, text: str) -> ExtractionResult:
        if self._closed:
            raise ValueError("tool call extractor is closed")
        self._pending += text
        visible: list[str] = []
        calls: list[ExtractedToolCall] = []
        while True:
            if self._inside:
                if self._consume_block(calls):
                    continue
                break
            if self._consume_visible(visible):
                continue
            break
        return ExtractionResult(text="".join(visible), tool_calls=tuple(calls))

    def finish(self) -> ExtractionResult:
        self._closed = True
        if self._inside:
            self._block = ""
            self._inside = False
            return ExtractionResult(
                text="",
                tool_calls=(ExtractedToolCall("", "", malformed=True),),
            )
        tail, self._pending = self._pending, ""
        return ExtractionResult(text=tail)

    def _consume_visible(self, visible: list[str]) -> bool:
        open_at = self._pending.find(TOOL_CALL_OPEN)
        if open_at >= 0:
            visible.append(self._pending[:open_at])
            self._pending = self._pending[open_at + len(TOOL_CALL_OPEN) :]
            self._inside = True
            self._block = ""
            return True
        held = held_suffix(self._pending, _TOOL_TAGS)
        split = len(self._pending) - held
        visible.append(self._pending[:split])
        self._pending = self._pending[split:]
        return False

    def _consume_block(self, calls: list[ExtractedToolCall]) -> bool:
        close_at = self._pending.find(TOOL_CALL_CLOSE)
        if close_at >= 0:
            self._block += self._pending[:close_at]
            self._pending = self._pending[close_at + len(TOOL_CALL_CLOSE) :]
            self._inside = False
            calls.append(_parse_block(self._block))
            self._block = ""
            return True
        held = held_suffix(self._pending, _TOOL_TAGS)
        split = len(self._pending) - held
        self._block += self._pending[:split]
        self._pending = self._pending[split:]
        if len(self._block) > MAX_TOOL_BLOCK_CHARS:
            self._block = ""
            self._inside = False
            calls.append(ExtractedToolCall("", "", malformed=True))
            return True
        return False


_FUNCTION_PATTERN: Final[re.Pattern[str]] = re.compile(r"<function=([^>\s]+)\s*>")
_PARAMETER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"<parameter=([^>\s]+)\s*>(.*?)</parameter\s*>", re.DOTALL
)


def _coerce(raw: str) -> object:
    """Convert a parameter to a scalar when it parses as one, otherwise keep the text.

    The XML form carries no types, so `importance` and `delta` would never satisfy the
    schema without this. The known cost is that free text which is entirely numeric, such
    as a memory whose content is "123", becomes a number and is then rejected by the
    validator. That is preferred over accepting numbers as strings, which would weaken the
    schema for every call.
    """

    stripped = raw.strip()
    try:
        value = json.loads(stripped)
    except (ValueError, RecursionError):
        return stripped
    return value if isinstance(value, int | float | bool) else stripped


def _parse_qwen_xml_block(block: str) -> ExtractedToolCall | None:
    """Parse Qwen's XML tool-call form, which is not JSON.

    Qwen3.5 emits `<function=name><parameter=key>value</parameter>...</function>`, the
    shape the official `qwen3_coder` tool-call parser expects.
    """

    match = _FUNCTION_PATTERN.search(block)
    if match is None:
        return None
    name = match.group(1).strip()
    if not name:
        return ExtractedToolCall("", "", malformed=True)
    arguments = {
        key.strip(): _coerce(value)
        for key, value in _PARAMETER_PATTERN.findall(block)
        if key.strip()
    }
    return ExtractedToolCall(name, json.dumps(arguments, ensure_ascii=False))


def _parse_block(block: str) -> ExtractedToolCall:
    xml_call = _parse_qwen_xml_block(block)
    if xml_call is not None:
        return xml_call
    try:
        payload = json.loads(block.strip())
    except (ValueError, RecursionError):
        return ExtractedToolCall("", "", malformed=True)
    if not isinstance(payload, dict):
        return ExtractedToolCall("", "", malformed=True)
    name = payload.get("name")
    arguments = payload.get("arguments")
    if not isinstance(name, str) or not name:
        return ExtractedToolCall("", "", malformed=True)
    if isinstance(arguments, str):
        return ExtractedToolCall(name, arguments)
    if isinstance(arguments, dict):
        return ExtractedToolCall(name, json.dumps(arguments, ensure_ascii=False))
    return ExtractedToolCall(name, "", malformed=True)
