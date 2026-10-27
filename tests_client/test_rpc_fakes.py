# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""The scriptable RPC double behaves like pyGdbServer for the real client transport."""

import asyncio
from typing import Any

import pytest

from pyGdbClient.rpc import JsonRpcClient, RpcError
from rpc_fakes import FakeRpcServer, RpcFailure


def test_fake_server_answers_records_and_fails_explicitly() -> None:
    """Fixed, computed, failing, and unscripted methods reach the real client."""

    async def exercise() -> tuple[Any, Any, list[int], list[tuple[str, dict[str, Any]]]]:
        server = FakeRpcServer(
            {
                "server.status": {"ready": True},
                "command.execute": lambda params: {"output": [f"ran:{params['command']}"]},
                "target.status": RpcFailure(-32000, "probe lost"),
            }
        )
        async with server:
            client = JsonRpcClient(server.url)
            await client.connect()
            try:
                status = await client.request("server.status")
                command = await client.request("command.execute", {"command": "lscpu"})
                codes: list[int] = []
                for method in ("target.status", "unscripted"):
                    with pytest.raises(RpcError) as error:
                        await client.request(method)
                    codes.append(error.value.code)
                await client.notify("logs.subscribe")
                await asyncio.sleep(0.05)
            finally:
                await client.close()
        return status, command, codes, server.requests

    status, command, codes, requests = asyncio.run(exercise())

    assert status == {"ready": True}
    assert command == {"output": ["ran:lscpu"]}
    assert codes == [-32000, -32601]
    assert requests == [
        ("server.status", {}),
        ("command.execute", {"command": "lscpu"}),
        ("target.status", {}),
        ("unscripted", {}),
        ("logs.subscribe", {}),
    ]


def test_fake_server_broadcasts_notifications() -> None:
    """Server-initiated notifications are queued by the client transport."""

    async def exercise() -> dict[str, Any]:
        async with FakeRpcServer({"logs.subscribe": {"subscribed": True}}) as server:
            client = JsonRpcClient(server.url)
            await client.connect()
            try:
                await client.request("logs.subscribe")
                await server.notify("log.event", {"source": "ocd", "message": "probe ready"})
                return await asyncio.wait_for(client.notifications.get(), 1)
            finally:
                await client.close()

    notification = asyncio.run(exercise())

    assert notification["method"] == "log.event"
    assert notification["params"] == {"source": "ocd", "message": "probe ready"}
    assert FakeRpcServer().methods == []
