"""Versioned, bounded contracts for Lune's loopback WebSocket IPC.

The transport owns only the authenticated envelope.  Command parameters and
successful results deliberately remain opaque JSON values: the authenticated
UI needs to carry private conversation, memory, and profile data without
putting that data in logs or unauthenticated responses.  The application layer
owns the semantic schema for those values.
"""

from __future__ import annotations

import json
import math
import re
import secrets
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Final, Literal

PROTOCOL_VERSION: Final = 1
LOOPBACK_HOST: Final = "127.0.0.1"

# These bounds apply before a message reaches an application handler.  They
# make a malformed local client unable to turn IPC into an unbounded memory or
# recursion sink.  Larger UI datasets must be paginated by the command layer.
MAX_MESSAGE_BYTES: Final = 64 * 1024
MAX_JSON_DEPTH: Final = 16
MAX_JSON_COLLECTION_ITEMS: Final = 512
MAX_JSON_STRING_BYTES: Final = 16 * 1024
MAX_JSON_KEY_BYTES: Final = 128
MAX_TOKEN_BYTES: Final = 128
MAX_REQUEST_ID_LENGTH: Final = 64
MAX_SAFE_JSON_INTEGER: Final = (2**53) - 1

HANDSHAKE_MESSAGE_TYPE: Final = "hello"
HELLO_ACK_MESSAGE_TYPE: Final = "hello_ack"
COMMAND_MESSAGE_TYPE: Final = "command"
RESULT_MESSAGE_TYPE: Final = "result"
ERROR_MESSAGE_TYPE: Final = "error"
EVENT_MESSAGE_TYPE: Final = "event"

type CommandName = str
type EventName = str
type ErrorCode = Literal[
    "invalid_request",
    "invalid_command",
    "invalid_params",
    "command_rejected",
    "handler_failed",
    "internal_error",
]
type JSONScalar = None | bool | int | float | str
type JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]

COMMAND_NAMES: Final[frozenset[str]] = frozenset({"set_microphone", "get_status", "shutdown"})
EVENT_NAMES: Final[frozenset[str]] = frozenset(
    {"state_changed", "device_changed", "budget_changed", "error"}
)
UI_COMMAND_NAMES: Final[frozenset[str]] = COMMAND_NAMES | frozenset(
    {
        "create_thread",
        "select_thread",
        "rename_thread",
        "submit_text",
        "set_text_speech",
        "forget_memory",
        "search_memories",
        "save_user_profile",
        "save_persona",
        "check_local_runtime",
        "check_audio_devices",
        "request_microphone_access",
        "set_voice",
    }
)
UI_EVENT_NAMES: Final[frozenset[str]] = EVENT_NAMES | frozenset(
    {"snapshot", "thread_updated", "message_added", "memory_updated"}
)
ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "invalid_request",
        "invalid_command",
        "invalid_params",
        "command_rejected",
        "handler_failed",
        "internal_error",
    }
)

_REQUEST_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9._:-]{{0,{MAX_REQUEST_ID_LENGTH - 1}}}"
)
_MESSAGE_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z][a-z0-9_]{0,63}")


class IPCProtocolError(ValueError):
    """A deliberately opaque client-facing protocol failure.

    ``code`` is an allowlisted value rather than a parser or application error.
    No constructor accepts untrusted request content, so using this exception
    cannot accidentally reflect private data to the peer.
    """

    __slots__ = ("code",)

    def __init__(self, code: ErrorCode = "invalid_request") -> None:
        self.code = code
        super().__init__(code)


class CommandRejected(Exception):
    """Allow a command handler to return a safe, finite error code."""

    __slots__ = ("code",)

    def __init__(self, code: ErrorCode = "command_rejected") -> None:
        if code not in ERROR_CODES:
            raise ValueError("command rejection code is not allowlisted")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class IPCConnectionInfo:
    """The one-time handoff data supplied to the trusted UI process."""

    port: int
    token: str = field(repr=False)
    protocol: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65_535:
            raise ValueError("IPC port must be in the TCP port range")
        if self.protocol != PROTOCOL_VERSION:
            raise ValueError("IPC protocol version is unsupported")
        if not self.token or _utf8_size(self.token) > MAX_TOKEN_BYTES:
            raise ValueError("IPC token has an invalid size")

    @property
    def url(self) -> str:
        return f"ws://{LOOPBACK_HOST}:{self.port}"

    def handshake_payload(self) -> dict[str, int | str]:
        """Return the intentionally private, one-time process handoff payload."""

        return {"port": self.port, "protocol": self.protocol, "token": self.token}

    def handshake_json(self) -> str:
        return json.dumps(self.handshake_payload(), ensure_ascii=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class Hello:
    """The first and only authentication message accepted on a socket."""

    token: str = field(repr=False)
    protocol: int = PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class IPCCommand:
    """A validated command envelope with immutable top-level parameters."""

    request_id: str
    command: CommandName
    params: Mapping[str, JSONValue] = field(repr=False)


def new_token() -> str:
    """Generate a 256-bit, URL-safe token for one trusted UI connection."""

    return secrets.token_urlsafe(32)


def parse_json_message(raw: str) -> dict[str, JSONValue]:
    """Decode one bounded text message without accepting duplicate keys or NaN."""

    if not isinstance(raw, str):
        raise IPCProtocolError()
    try:
        if _utf8_size(raw) > MAX_MESSAGE_BYTES:
            raise IPCProtocolError()
        decoded = json.loads(
            raw,
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except (json.JSONDecodeError, RecursionError, TypeError, UnicodeEncodeError, ValueError):
        raise IPCProtocolError() from None
    normalized = normalize_json_value(decoded)
    if not isinstance(normalized, dict):
        raise IPCProtocolError()
    return normalized


def parse_hello(payload: Mapping[str, JSONValue]) -> Hello:
    """Validate the exact initial hello envelope, without authenticating it."""

    _require_exact_fields(payload, frozenset({"type", "protocol", "token"}))
    if payload["type"] != HANDSHAKE_MESSAGE_TYPE:
        raise IPCProtocolError()
    protocol = payload["protocol"]
    if type(protocol) is not int or protocol != PROTOCOL_VERSION:
        raise IPCProtocolError()
    token = payload["token"]
    if not isinstance(token, str) or not token or _utf8_size(token) > MAX_TOKEN_BYTES:
        raise IPCProtocolError()
    return Hello(token=token, protocol=protocol)


def parse_command(
    payload: Mapping[str, JSONValue],
    *,
    allowed_commands: Collection[str] = COMMAND_NAMES,
) -> IPCCommand:
    """Validate the command envelope while leaving its JSON data opaque."""

    _require_exact_fields(payload, frozenset({"type", "id", "command", "params"}))
    if payload["type"] != COMMAND_MESSAGE_TYPE:
        raise IPCProtocolError()
    request_id = validate_request_id(payload["id"])
    raw_command = payload["command"]
    if not isinstance(raw_command, str) or raw_command not in allowed_commands:
        raise IPCProtocolError("invalid_command")
    params = payload["params"]
    if not isinstance(params, dict):
        raise IPCProtocolError("invalid_params")
    return IPCCommand(
        request_id=request_id,
        command=raw_command,
        params=MappingProxyType(params),
    )


def optional_request_id(payload: Mapping[str, JSONValue]) -> str | None:
    """Return a safely echoable request ID, if the malformed envelope had one."""

    try:
        return validate_request_id(payload.get("id"))
    except IPCProtocolError:
        return None


def normalize_command_names(names: Collection[str]) -> frozenset[str]:
    return _normalize_message_names(names)


def normalize_event_names(names: Collection[str]) -> frozenset[str]:
    return _normalize_message_names(names)


def validate_event_name(
    value: object,
    *,
    allowed_events: Collection[str] = EVENT_NAMES,
) -> EventName:
    if not isinstance(value, str) or value not in allowed_events:
        raise IPCProtocolError()
    return value


def validate_request_id(value: object) -> str:
    if not isinstance(value, str) or _REQUEST_ID_PATTERN.fullmatch(value) is None:
        raise IPCProtocolError()
    return value


def normalize_json_value(value: object) -> JSONValue:
    """Return a bounded JSON-only copy suitable for an authenticated wire frame."""

    return _normalize_json_value(value, depth=0, ancestors=set())


def hello_ack_message() -> str:
    return encode_message({"type": HELLO_ACK_MESSAGE_TYPE, "protocol": PROTOCOL_VERSION})


def result_message(request_id: str, result: object) -> str:
    return encode_message(
        {
            "type": RESULT_MESSAGE_TYPE,
            "id": validate_request_id(request_id),
            "result": normalize_json_value(result),
        }
    )


def error_message(request_id: str, code: ErrorCode) -> str:
    if code not in ERROR_CODES:
        raise ValueError("IPC error code is not allowlisted")
    return encode_message(
        {
            "type": ERROR_MESSAGE_TYPE,
            "id": validate_request_id(request_id),
            "code": code,
        }
    )


def event_message(
    event: object,
    payload: object,
    *,
    allowed_events: Collection[str] = EVENT_NAMES,
) -> str:
    return encode_message(
        {
            "type": EVENT_MESSAGE_TYPE,
            "event": validate_event_name(event, allowed_events=allowed_events),
            "payload": normalize_json_value(payload),
        }
    )


def encode_message(payload: Mapping[str, object]) -> str:
    """Encode a bounded JSON object and fail without serializing unsafe values."""

    normalized = normalize_json_value(payload)
    if not isinstance(normalized, dict):
        raise IPCProtocolError()
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if _utf8_size(encoded) > MAX_MESSAGE_BYTES:
            raise IPCProtocolError()
    except (TypeError, UnicodeEncodeError, ValueError):
        raise IPCProtocolError() from None
    return encoded


def _require_exact_fields(payload: Mapping[str, JSONValue], expected: frozenset[str]) -> None:
    if set(payload) != expected:
        raise IPCProtocolError()


def _normalize_message_names(names: Collection[str]) -> frozenset[str]:
    if isinstance(names, str):
        raise ValueError("IPC allowlist must be a collection of names")
    normalized = frozenset(names)
    if not normalized or any(
        not isinstance(name, str) or _MESSAGE_NAME_PATTERN.fullmatch(name) is None
        for name in normalized
    ):
        raise ValueError("IPC allowlist contains an invalid name")
    return normalized


def _normalize_json_value(
    value: object,
    *,
    depth: int,
    ancestors: set[int],
) -> JSONValue:
    if depth > MAX_JSON_DEPTH:
        raise IPCProtocolError()
    if value is None:
        return None
    if type(value) is bool:
        return value
    if type(value) is int:
        integer = value
        if abs(integer) > MAX_SAFE_JSON_INTEGER:
            raise IPCProtocolError()
        return integer
    if type(value) is float:
        number = value
        if not math.isfinite(number) or abs(number) > MAX_SAFE_JSON_INTEGER:
            raise IPCProtocolError()
        return number
    if isinstance(value, str):
        if _utf8_size(value) > MAX_JSON_STRING_BYTES:
            raise IPCProtocolError()
        return value
    if isinstance(value, Mapping):
        return _normalize_mapping(value, depth=depth, ancestors=ancestors)
    if isinstance(value, list | tuple):
        return _normalize_sequence(value, depth=depth, ancestors=ancestors)
    raise IPCProtocolError()


def _normalize_mapping(
    value: Mapping[object, object],
    *,
    depth: int,
    ancestors: set[int],
) -> dict[str, JSONValue]:
    identity = id(value)
    if identity in ancestors or len(value) > MAX_JSON_COLLECTION_ITEMS:
        raise IPCProtocolError()
    ancestors.add(identity)
    try:
        normalized: dict[str, JSONValue] = {}
        for key, item in value.items():
            if not isinstance(key, str) or _utf8_size(key) > MAX_JSON_KEY_BYTES:
                raise IPCProtocolError()
            normalized[key] = _normalize_json_value(
                item,
                depth=depth + 1,
                ancestors=ancestors,
            )
        return normalized
    finally:
        ancestors.remove(identity)


def _normalize_sequence(
    value: list[object] | tuple[object, ...],
    *,
    depth: int,
    ancestors: set[int],
) -> list[JSONValue]:
    identity = id(value)
    if identity in ancestors or len(value) > MAX_JSON_COLLECTION_ITEMS:
        raise IPCProtocolError()
    ancestors.add(identity)
    try:
        return [_normalize_json_value(item, depth=depth + 1, ancestors=ancestors) for item in value]
    finally:
        ancestors.remove(identity)


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate JSON key")
        payload[key] = value
    return payload


def _reject_non_json_constant(_: str) -> None:
    raise ValueError("non-JSON numeric constant")


def _utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))
