# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Scriptable loopback JSON-RPC server standing in for pyGdbServer."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
from types import TracebackType
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve


@dataclass(frozen=True)
class RpcFailure:
    """A scripted JSON-RPC error response."""

    code: int
    message: str


type RpcResponse = RpcFailure | Callable[[dict[str, Any]], Any] | Any


class FakeRpcServer:
    """Answer scripted methods, record every request, and broadcast notifications.

    A response is a fixed result, an :class:`RpcFailure`, or a callable receiving
    the request parameters. Unscripted methods return ``-32601``, so an unexpected
    request fails visibly instead of receiving a plausible default.
    """

    def __init__(self, responses: Mapping[str, RpcResponse] | None = None) -> None:
        self.responses: dict[str, RpcResponse] = dict(responses or {})
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self._connections: set[ServerConnection] = set()
        self._server: Server | None = None
        self.url = ""

    @property
    def methods(self) -> list[str]:
        """Received method names, in arrival order."""
        return [method for method, _ in self.requests]

    async def __aenter__(self) -> FakeRpcServer:
        """Listen on an ephemeral loopback port."""
        self._server = await serve(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the listener and every open connection."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def notify(self, method: str, params: Mapping[str, Any]) -> None:
        """Send a JSON-RPC notification to every connected client."""
        payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": dict(params)})
        for connection in tuple(self._connections):
            await connection.send(payload)

    async def _handle(self, websocket: ServerConnection) -> None:
        self._connections.add(websocket)
        try:
            async for payload in websocket:
                request = json.loads(payload)
                params = dict(request.get("params", {}))
                self.requests.append((request["method"], params))
                if "id" in request:
                    await websocket.send(json.dumps(self._reply(request, params)))
        finally:
            self._connections.discard(websocket)

    def _reply(self, request: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        response: dict[str, Any] = {"jsonrpc": "2.0", "id": request["id"]}
        method = request["method"]
        if method not in self.responses:
            response["error"] = {"code": -32601, "message": f"Method not found: {method}"}
            return response
        scripted = self.responses[method]
        result = scripted(params) if callable(scripted) else scripted
        if isinstance(result, RpcFailure):
            response["error"] = {"code": result.code, "message": result.message}
        else:
            response["result"] = result
        return response
