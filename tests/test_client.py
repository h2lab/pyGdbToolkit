# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Client transport and Textual dashboard tests."""

import asyncio
import json
from typing import Any

from textual.widgets import Tree
from websockets.asyncio.server import ServerConnection, serve

from pyGdbClient.app import PyGdbClientApp
from pyGdbClient.rpc import JsonRpcClient, RpcError


async def _respond(websocket: ServerConnection, request: dict[str, Any]) -> None:
    method = request["method"]
    params: dict[str, Any] = {}
    error = None
    if method == "command.execute":
        command = request["params"]["command"]
        params = {
            "class": "done",
            "record": "done",
            "output": [f"rendered:{command}"],
        }
    elif method == "server.status":
        params = {
            "ready": True,
            "gdb": {"interpreter": "mi3"},
            "ocd": {"gdb_port": 34567},
        }
    elif method == "target.status":
        params = {"state": "stopped", "thread_id": "1", "core": "0", "access_port": "AP#0"}
    elif method == "svd.peripherals":
        params = {
            "loaded": True,
            "device": "TestDevice",
            "peripherals": [
                {
                    "name": "GPIOA",
                    "description": "GPIO port A",
                    "base_address": 0x48000000,
                    "registers": [{"name": "MODER", "address_offset": 0, "description": "Mode"}],
                }
            ],
        }
    elif method == "missing":
        error = {"code": -32601, "message": "Method not found"}
    elif method == "logs.subscribe":
        params = {"subscribed": True}

    if "id" in request:
        response: dict[str, Any] = {"jsonrpc": "2.0", "id": request["id"]}
        if error is not None:
            response["error"] = error
        else:
            response["result"] = params
        await websocket.send(json.dumps(response))
        if method == "logs.subscribe":
            await websocket.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "log.event",
                        "params": {"source": "ocd", "stream": "stderr", "message": "probe ready"},
                    }
                )
            )


def test_rpc_client_correlates_responses_and_notifications() -> None:
    """The transport returns request results and queues independent notifications."""

    async def exercise() -> tuple[Any, dict[str, Any], bool]:
        async def handler(websocket: ServerConnection) -> None:
            async for payload in websocket:
                await _respond(websocket, json.loads(payload))

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = JsonRpcClient(f"ws://127.0.0.1:{port}")
            await client.connect()
            try:
                result = await client.request("server.status")
                await client.request("logs.subscribe")
                notification = await asyncio.wait_for(client.notifications.get(), 1)
                try:
                    await client.request("missing")
                except RpcError as error:
                    failed = error.code == -32601
                else:
                    failed = False
                return result, notification, failed
            finally:
                await client.close()

    result, notification, failed = asyncio.run(exercise())

    assert result["ready"] is True
    assert notification["params"]["message"] == "probe ready"
    assert failed


def test_textual_dashboard_populates_tree_and_shows_expanded_peripheral() -> None:
    """Opening a peripheral node sends its SVD show command through the client."""

    async def exercise() -> tuple[list[str], str]:
        commands: list[str] = []

        async def handler(websocket: ServerConnection) -> None:
            async for payload in websocket:
                request = json.loads(payload)
                if request["method"] == "command.execute":
                    commands.append(request["params"]["command"])
                await _respond(websocket, request)

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            app = PyGdbClientApp(f"ws://127.0.0.1:{port}")
            async with app.run_test(size=(150, 48)) as pilot:
                await pilot.pause(0.5)
                tree = app.query_one("#svd-tree", Tree)
                assert len(tree.root.children) == 1
                assert tree.root.children[0].label.plain.startswith("GPIOA")
                summary = app.query_one("#target-summary")
                assert summary.lines
                await pilot.click(tree, offset=(3, 2))
                await pilot.pause(0.2)
                state = app.query_one("#target-state").render()
                return commands, str(state)

    commands, state_parts = asyncio.run(exercise())

    assert "lscpu" in commands
    assert "svd load" in commands
    assert "svd show GPIOA" in commands
    assert "BREAK" in state_parts
