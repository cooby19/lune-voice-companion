from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, cast

import pytest
from websockets.asyncio.client import ClientConnection, connect

from lune.ipc import (
    MAX_JSON_DEPTH,
    MAX_MESSAGE_BYTES,
    BroadcastResult,
    CommandName,
    IPCProtocolError,
    JSONValue,
    LoopbackIPCServer,
)


async def receive_json(connection: ClientConnection) -> dict[str, object]:
    message = await connection.recv()
    assert isinstance(message, str)
    decoded = json.loads(message)
    assert isinstance(decoded, dict)
    return decoded


async def authenticate(connection: ClientConnection, *, token: str) -> None:
    await connection.send(json.dumps({"type": "hello", "protocol": 1, "token": token}))
    assert await receive_json(connection) == {"type": "hello_ack", "protocol": 1}


async def wait_for_policy_close(connection: ClientConnection) -> None:
    await asyncio.wait_for(connection.wait_closed(), timeout=1.0)
    assert connection.close_code == 1008
    assert connection.close_reason == "unauthorized"


@pytest.mark.asyncio
async def test_authenticated_command_reaches_injected_handler_and_keeps_result_opaque() -> None:
    observed: list[tuple[CommandName, Mapping[str, JSONValue]]] = []

    async def handle(command: CommandName, params: Mapping[str, JSONValue]) -> JSONValue:
        observed.append((command, params))
        return {
            "state": "listening",
            "threads": [{"id": "thread-1", "messages": ["private text"]}],
        }

    server = LoopbackIPCServer(handle)
    info = await server.start()
    try:
        assert info.url == f"ws://127.0.0.1:{info.port}"
        assert json.loads(info.handshake_json()) == info.handshake_payload()
        async with connect(info.url, proxy=None) as connection:
            await authenticate(connection, token=info.token)
            await connection.send(
                json.dumps(
                    {
                        "type": "command",
                        "id": "cmd-1",
                        "command": "get_status",
                        "params": {"include": ["threads", "memory"]},
                    }
                )
            )
            assert await receive_json(connection) == {
                "type": "result",
                "id": "cmd-1",
                "result": {
                    "state": "listening",
                    "threads": [{"id": "thread-1", "messages": ["private text"]}],
                },
            }
        assert observed == [("get_status", {"include": ["threads", "memory"]})]
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_missing_wrong_and_replayed_tokens_are_all_rejected_without_detail() -> None:
    async def handle(_: CommandName, __: Mapping[str, JSONValue]) -> JSONValue:
        return {}

    server = LoopbackIPCServer(handle, handshake_timeout_s=0.1)
    info = await server.start()
    try:
        async with connect(info.url, proxy=None) as no_hello:
            await wait_for_policy_close(no_hello)

        async with connect(info.url, proxy=None) as missing_token:
            await missing_token.send(json.dumps({"type": "hello", "protocol": 1}))
            await wait_for_policy_close(missing_token)

        async with connect(info.url, proxy=None) as wrong_token:
            await wrong_token.send(json.dumps({"type": "hello", "protocol": 1, "token": "wrong"}))
            await wait_for_policy_close(wrong_token)

        async with connect(info.url, proxy=None) as first_connection:
            await authenticate(first_connection, token=info.token)

        async with connect(info.url, proxy=None) as replay:
            await replay.send(json.dumps({"type": "hello", "protocol": 1, "token": info.token}))
            await wait_for_policy_close(replay)
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_malformed_envelopes_get_finite_errors_and_handler_exceptions_do_not_leak() -> None:
    private_error = "api_key=never-return-this"

    async def handle(command: CommandName, _: Mapping[str, JSONValue]) -> JSONValue:
        if command == "shutdown":
            raise RuntimeError(private_error)
        return cast(JSONValue, {"unexpected": object()})

    server = LoopbackIPCServer(handle)
    info = await server.start()
    try:
        async with connect(info.url, proxy=None) as connection:
            await authenticate(connection, token=info.token)
            await connection.send(
                json.dumps(
                    {
                        "type": "command",
                        "id": "extra-field",
                        "command": "get_status",
                        "params": {},
                        "token": info.token,
                    }
                )
            )
            assert await receive_json(connection) == {
                "type": "error",
                "id": "extra-field",
                "code": "invalid_request",
            }

            await connection.send(
                json.dumps(
                    {
                        "type": "command",
                        "id": "unknown-command",
                        "command": "delete_everything",
                        "params": {},
                    }
                )
            )
            assert await receive_json(connection) == {
                "type": "error",
                "id": "unknown-command",
                "code": "invalid_command",
            }

            await connection.send(
                json.dumps(
                    {
                        "type": "command",
                        "id": "bad-params",
                        "command": "get_status",
                        "params": [],
                    }
                )
            )
            assert await receive_json(connection) == {
                "type": "error",
                "id": "bad-params",
                "code": "invalid_params",
            }

            await connection.send(
                json.dumps(
                    {
                        "type": "command",
                        "id": "invalid-result",
                        "command": "get_status",
                        "params": {},
                    }
                )
            )
            assert await receive_json(connection) == {
                "type": "error",
                "id": "invalid-result",
                "code": "internal_error",
            }

            await connection.send(
                json.dumps(
                    {
                        "type": "command",
                        "id": "handler-error",
                        "command": "shutdown",
                        "params": {},
                    }
                )
            )
            response = await receive_json(connection)
            assert response == {
                "type": "error",
                "id": "handler-error",
                "code": "handler_failed",
            }
            assert private_error not in json.dumps(response)
            assert info.token not in json.dumps(response)
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_only_authenticated_clients_receive_bounded_events() -> None:
    async def handle(_: CommandName, __: Mapping[str, JSONValue]) -> JSONValue:
        return {}

    server = LoopbackIPCServer(handle)
    info = await server.start()
    try:
        assert await server.broadcast("state_changed", {"state": "mic_off"}) == BroadcastResult(
            0,
            0,
            0,
        )
        async with connect(info.url, proxy=None) as connection:
            await authenticate(connection, token=info.token)
            delivery = await server.broadcast(
                "state_changed",
                {"state": "listening", "thread": {"id": "thread-1"}},
            )
            assert delivery == BroadcastResult(attempted=1, delivered=1, dropped=0)
            assert await receive_json(connection) == {
                "type": "event",
                "event": "state_changed",
                "payload": {"state": "listening", "thread": {"id": "thread-1"}},
            }

            with pytest.raises(IPCProtocolError):
                await server.broadcast("message_added", {"message": "no schema bypass"})
            with pytest.raises(IPCProtocolError):
                await server.broadcast("state_changed", {"not_json": object()})
    finally:
        await server.close()


def test_json_limits_reject_deep_and_oversized_payloads() -> None:
    from lune.ipc.contracts import normalize_json_value

    deeply_nested: Any = {}
    cursor = deeply_nested
    for _ in range(MAX_JSON_DEPTH + 1):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child
    with pytest.raises(IPCProtocolError):
        normalize_json_value(deeply_nested)
    with pytest.raises(IPCProtocolError):
        normalize_json_value("x" * (MAX_MESSAGE_BYTES + 1))


@pytest.mark.asyncio
async def test_ui_host_can_explicitly_extend_fixed_command_and_event_allowlists() -> None:
    observed: list[CommandName] = []

    async def handle(command: CommandName, _: Mapping[str, JSONValue]) -> JSONValue:
        observed.append(command)
        return {"accepted": True}

    server = LoopbackIPCServer(
        handle,
        command_names=frozenset({"get_status", "submit_text"}),
        event_names=frozenset({"state_changed", "message_added"}),
    )
    info = await server.start()
    try:
        async with connect(info.url, proxy=None) as connection:
            await authenticate(connection, token=info.token)
            await connection.send(
                json.dumps(
                    {
                        "type": "command",
                        "id": "submit-1",
                        "command": "submit_text",
                        "params": {"text": "你好"},
                    }
                )
            )
            assert await receive_json(connection) == {
                "type": "result",
                "id": "submit-1",
                "result": {"accepted": True},
            }
            assert observed == ["submit_text"]

            delivery = await server.broadcast("message_added", {"thread_id": "thread-1"})
            assert delivery == BroadcastResult(attempted=1, delivered=1, dropped=0)
            assert await receive_json(connection) == {
                "type": "event",
                "event": "message_added",
                "payload": {"thread_id": "thread-1"},
            }

            await connection.send(
                json.dumps(
                    {
                        "type": "command",
                        "id": "not-allowed",
                        "command": "shutdown",
                        "params": {},
                    }
                )
            )
            assert await receive_json(connection) == {
                "type": "error",
                "id": "not-allowed",
                "code": "invalid_command",
            }
    finally:
        await server.close()
