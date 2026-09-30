# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Asynchronous JSON-RPC 2.0 client over WebSocket."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import json
from typing import Any

from websockets.asyncio.client import ClientConnection, connect


class RpcError(Exception):
    """A JSON-RPC error response from pyGdbServer."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class JsonRpcClient:
    """Multiplex request responses and server notifications on one socket."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.websocket: ClientConnection | None = None
        self.notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2_000)
        self._reader_task: asyncio.Task[None] | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._send_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open the WebSocket and start receiving responses and notifications."""
        if self.websocket is not None:
            return
        self.websocket = await connect(self.url, max_size=8 * 1024 * 1024)
        self._reader_task = asyncio.create_task(self._read_messages())

    async def request(
        self, method: str, params: Mapping[str, Any] | None = None, timeout: float = 60.0
    ) -> Any:
        """Send one JSON-RPC request and await its matching response."""
        if self.websocket is None:
            raise ConnectionError("not connected to pyGdbServer")
        self._request_id += 1
        request_id = self._request_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": dict(params or {}),
        }
        try:
            async with self._send_lock:
                await self.websocket.send(json.dumps(message, ensure_ascii=False))
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification without waiting for a response."""
        if self.websocket is None:
            raise ConnectionError("not connected to pyGdbServer")
        message = {"jsonrpc": "2.0", "method": method, "params": dict(params or {})}
        async with self._send_lock:
            await self.websocket.send(json.dumps(message, ensure_ascii=False))

    async def _read_messages(self) -> None:
        assert self.websocket is not None
        try:
            async for payload in self.websocket:
                message = json.loads(payload)
                if not isinstance(message, dict):
                    continue
                if "id" in message:
                    future = self._pending.get(message["id"])
                    if future is None or future.done():
                        continue
                    if "error" in message:
                        error = message["error"]
                        future.set_exception(
                            RpcError(
                                int(error.get("code", -32000)),
                                str(error.get("message", "JSON-RPC request failed")),
                                error.get("data"),
                            )
                        )
                    else:
                        future.set_result(message.get("result"))
                else:
                    try:
                        self.notifications.put_nowait(message)
                    except asyncio.QueueFull:
                        pass
        except Exception as error:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError(f"WebSocket disconnected: {error}"))
            self._pending.clear()

    async def close(self) -> None:
        """Close the WebSocket and stop the reader task."""
        if self.websocket is not None:
            await self.websocket.close()
            self.websocket = None
        if self._reader_task is not None:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
