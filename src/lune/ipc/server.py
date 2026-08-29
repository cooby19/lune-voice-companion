"""Authenticated, one-client loopback WebSocket IPC server."""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Final

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from lune.ipc.contracts import (
    COMMAND_NAMES,
    EVENT_NAMES,
    LOOPBACK_HOST,
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    CommandName,
    CommandRejected,
    EventName,
    IPCCommand,
    IPCConnectionInfo,
    IPCProtocolError,
    JSONValue,
    error_message,
    event_message,
    hello_ack_message,
    new_token,
    normalize_command_names,
    normalize_event_names,
    optional_request_id,
    parse_command,
    parse_hello,
    parse_json_message,
    result_message,
)

HANDSHAKE_TIMEOUT_S: Final = 2.0
SEND_TIMEOUT_S: Final = 1.0
POLICY_VIOLATION_CLOSE_CODE: Final = 1008
SERVER_SHUTDOWN_CLOSE_CODE: Final = 1001

type CommandHandler = Callable[
    [CommandName, Mapping[str, JSONValue]],
    JSONValue | Awaitable[JSONValue],
]


@dataclass(frozen=True, slots=True)
class BroadcastResult:
    """Delivery counts only; it never retains event payloads or client details."""

    attempted: int
    delivered: int
    dropped: int


@dataclass(eq=False, slots=True)
class _AuthenticatedClient:
    connection: ServerConnection
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: str) -> bool:
        try:
            async with self._send_lock:
                await asyncio.wait_for(self.connection.send(message), timeout=SEND_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return True

    async def close(self, *, code: int, reason: str) -> None:
        try:
            async with self._send_lock:
                await asyncio.wait_for(
                    self.connection.close(code=code, reason=reason),
                    timeout=SEND_TIMEOUT_S,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            return


class LoopbackIPCServer:
    """Serve one authenticated Web UI over an ephemeral IPv4 loopback port.

    The instance intentionally consumes its random token after the first valid
    hello.  A disconnected UI cannot reconnect with the same token; a caller
    must create a new server/process handoff instead.  This prevents a stale
    token from becoming a reusable local credential.
    """

    def __init__(
        self,
        command_handler: CommandHandler,
        *,
        command_names: Collection[str] = COMMAND_NAMES,
        event_names: Collection[str] = EVENT_NAMES,
        handshake_timeout_s: float = HANDSHAKE_TIMEOUT_S,
    ) -> None:
        if handshake_timeout_s <= 0:
            raise ValueError("handshake timeout must be positive")
        self._command_handler = command_handler
        self._command_names = normalize_command_names(command_names)
        self._event_names = normalize_event_names(event_names)
        self._handshake_timeout_s = handshake_timeout_s
        self._token = new_token()
        self._token_consumed = False
        self._server: Server | None = None
        self._connection_info: IPCConnectionInfo | None = None
        self._clients: set[_AuthenticatedClient] = set()
        self._authentication_lock = asyncio.Lock()
        self._clients_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._closed = False

    @property
    def connection_info(self) -> IPCConnectionInfo:
        if self._connection_info is None:
            raise RuntimeError("IPC server has not started")
        return self._connection_info

    @property
    def running(self) -> bool:
        return self._server is not None and not self._closed

    async def start(self) -> IPCConnectionInfo:
        """Bind exactly ``127.0.0.1:0`` and return the trusted handoff data."""

        async with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("IPC server is closed")
            if self._server is not None:
                return self.connection_info
            server = await serve(
                self._handle_connection,
                LOOPBACK_HOST,
                0,
                compression=None,
                max_size=MAX_MESSAGE_BYTES,
                max_queue=8,
                open_timeout=self._handshake_timeout_s,
                close_timeout=SEND_TIMEOUT_S,
                server_header=None,
            )
            sockets = tuple(server.sockets)
            if not sockets:
                server.close()
                await server.wait_closed()
                raise RuntimeError("IPC server did not expose a listening socket")
            address = sockets[0].getsockname()
            port = int(address[1])
            self._server = server
            self._connection_info = IPCConnectionInfo(
                port=port,
                protocol=PROTOCOL_VERSION,
                token=self._token,
            )
            return self._connection_info

    async def close(self) -> None:
        """Close the listener and authenticated sockets without exposing errors."""

        async with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            server = self._server
            self._server = None
        async with self._clients_lock:
            clients = tuple(self._clients)
            self._clients.clear()
        await asyncio.gather(
            *(
                client.close(code=SERVER_SHUTDOWN_CLOSE_CODE, reason="server_shutdown")
                for client in clients
            )
        )
        if server is not None:
            server.close()
            await server.wait_closed()

    async def broadcast(self, event: EventName, payload: object) -> BroadcastResult:
        """Deliver one bounded event only to currently authenticated clients.

        A bad event value is rejected before any peer sees a partial payload.
        A slow or disconnected peer is dropped instead of blocking the engine's
        state machine indefinitely.
        """

        message = event_message(event, payload, allowed_events=self._event_names)
        async with self._clients_lock:
            clients = tuple(self._clients)
        delivered_flags = await asyncio.gather(*(client.send(message) for client in clients))
        dropped_clients = tuple(
            client
            for client, delivered in zip(clients, delivered_flags, strict=True)
            if not delivered
        )
        if dropped_clients:
            async with self._clients_lock:
                for client in dropped_clients:
                    self._clients.discard(client)
            await asyncio.gather(
                *(
                    client.close(code=SERVER_SHUTDOWN_CLOSE_CODE, reason="delivery_failed")
                    for client in dropped_clients
                )
            )
        delivered = len(clients) - len(dropped_clients)
        return BroadcastResult(
            attempted=len(clients),
            delivered=delivered,
            dropped=len(dropped_clients),
        )

    async def _handle_connection(self, connection: ServerConnection) -> None:
        client = await self._authenticate(connection)
        if client is None:
            return
        if not await client.send(hello_ack_message()):
            return
        async with self._clients_lock:
            if self._closed:
                await client.close(code=SERVER_SHUTDOWN_CLOSE_CODE, reason="server_shutdown")
                return
            self._clients.add(client)
        try:
            async for raw_message in connection:
                if not isinstance(raw_message, str):
                    await self._reject_connection(connection)
                    return
                try:
                    payload = parse_json_message(raw_message)
                except IPCProtocolError:
                    await self._reject_connection(connection)
                    return
                # A second hello is a token replay attempt, even when it arrives
                # on the same already-authenticated socket.
                if payload.get("type") == "hello":
                    await self._reject_connection(connection)
                    return
                request_id = optional_request_id(payload)
                if request_id is None:
                    await self._reject_connection(connection)
                    return
                try:
                    command = parse_command(payload, allowed_commands=self._command_names)
                except IPCProtocolError as error:
                    if not await client.send(error_message(request_id, error.code)):
                        return
                    continue
                await self._dispatch(client, command)
        except ConnectionClosed:
            return
        finally:
            await self._discard_client(client)

    async def _authenticate(self, connection: ServerConnection) -> _AuthenticatedClient | None:
        try:
            raw_message = await asyncio.wait_for(
                connection.recv(),
                timeout=self._handshake_timeout_s,
            )
            if not isinstance(raw_message, str):
                await self._reject_connection(connection)
                return None
            hello = parse_hello(parse_json_message(raw_message))
        except asyncio.CancelledError:
            raise
        except (ConnectionClosed, IPCProtocolError, TimeoutError):
            await self._reject_connection(connection)
            return None
        async with self._authentication_lock:
            authenticated = (
                not self._closed
                and not self._token_consumed
                and hmac.compare_digest(self._token, hello.token)
            )
            if authenticated:
                self._token_consumed = True
        if not authenticated:
            await self._reject_connection(connection)
            return None
        return _AuthenticatedClient(connection)

    async def _dispatch(self, client: _AuthenticatedClient, command: IPCCommand) -> None:
        try:
            result = self._command_handler(command.command, command.params)
            if isinstance(result, Awaitable):
                result = await result
            message = result_message(command.request_id, result)
        except asyncio.CancelledError:
            raise
        except CommandRejected as error:
            message = error_message(command.request_id, error.code)
        except IPCProtocolError:
            message = error_message(command.request_id, "internal_error")
        except Exception:
            # Handler exceptions often contain paths, tokens, or private text.
            # The wire contract intentionally exposes only this finite code.
            message = error_message(command.request_id, "handler_failed")
        await client.send(message)

    async def _discard_client(self, client: _AuthenticatedClient) -> None:
        async with self._clients_lock:
            self._clients.discard(client)

    async def _reject_connection(self, connection: ServerConnection) -> None:
        try:
            await asyncio.wait_for(
                connection.close(code=POLICY_VIOLATION_CLOSE_CODE, reason="unauthorized"),
                timeout=SEND_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return
