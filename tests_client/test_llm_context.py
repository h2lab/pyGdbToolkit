# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""The client decodes ``target.context`` strictly before giving it to an agent."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

from pyGdbClient.llm.context import (
    TargetContextError,
    fetch_target_context,
    parse_target_context,
)
from pyGdbClient.llm.contracts import ContextFact, MemoryRegion
from pyGdbClient.rpc import JsonRpcClient, RpcError
from rpc_fakes import FakeRpcServer, RpcFailure


def _fact(value: str | None, source: str = "configuration") -> dict[str, Any]:
    return {
        "value": value,
        "source": source,
        "unavailable_reason": None if value is not None else "not declared",
    }


SNAPSHOT: dict[str, Any] = {
    "schema_version": 1,
    "context_id": "ctx-0123456789abcdef",
    "mcu": _fact("Raspberry Pi RP2350", "toolkit-discovery"),
    "revision": _fact(None),
    "architecture": _fact("arm", "architecture-probe"),
    "debug_transport": _fact("SWD"),
    "ocd": _fact("openocd (openocd)"),
    "core": _fact("rp2350.cm0 (id 0)", "debug-server"),
    "memory_map": {
        "regions": [
            {
                "start": 0x20000000,
                "end": 0x20082000,
                "kind": "ram",
                "source": "gdb-server",
                "name": "SRAM",
                "protection": "rw",
            },
            {"start": 0, "end": 0x4000, "kind": "rom", "source": "svd"},
        ],
        "source": "toolkit-discovery",
        "unavailable_reason": None,
    },
    "svd_file": _fact("/srv/rp2350.svd", "svd"),
    "future_field": {"ignored": True},
}


def _mutated(path: tuple[str | int, ...], value: Any) -> dict[str, Any]:
    payload = copy.deepcopy(SNAPSHOT)
    container: Any = payload
    for key in path[:-1]:
        container = container[key]
    if value is KeyError:
        del container[path[-1]]
    else:
        container[path[-1]] = value
    return payload


def test_valid_snapshot_is_decoded_with_provenance() -> None:
    """Every fact keeps its source; unknown facts keep their reason."""
    context = parse_target_context(SNAPSHOT)

    assert context.context_id == "ctx-0123456789abcdef"
    assert context.mcu == ContextFact("Raspberry Pi RP2350", "toolkit-discovery")
    assert context.revision == ContextFact(None, "configuration", "not declared")
    assert context.debug_transport.value == "SWD"
    assert context.memory_map.regions == (
        MemoryRegion(0x20000000, 0x20082000, "ram", "gdb-server", "SRAM", "rw"),
        MemoryRegion(0, 0x4000, "rom", "svd"),
    )
    assert context.svd_file.value == "/srv/rp2350.svd"


def test_unavailable_memory_map_is_decoded() -> None:
    """A missing map stays distinct from an empty one."""
    payload = _mutated(
        ("memory_map",),
        {"regions": [], "source": "toolkit-discovery", "unavailable_reason": "run memmap"},
    )
    memory_map = parse_target_context(payload).memory_map
    assert memory_map.regions == ()
    assert memory_map.unavailable_reason == "run memmap"


@pytest.mark.parametrize(
    "payload,message",
    [
        ([], "must be an object"),
        (_mutated(("schema_version",), 2), "schema version"),
        (_mutated(("schema_version",), True), "schema version"),
        (_mutated(("schema_version",), KeyError), "schema version"),
        (_mutated(("context_id",), ""), "ID"),
        (_mutated(("context_id",), 3), "context_id"),
        (_mutated(("mcu",), KeyError), "mcu must be an object"),
        (_mutated(("ocd", "source"), ""), "ocd"),
        (_mutated(("ocd", "value"), 7), "ocd.value"),
        (_mutated(("core", "unavailable_reason"), "contradiction"), "core"),
        (_mutated(("memory_map",), None), "memory_map must be an object"),
        (_mutated(("memory_map", "regions"), {}), "regions must be an array"),
        (_mutated(("memory_map", "regions", 0, "start"), True), "start"),
        (_mutated(("memory_map", "regions", 0, "end"), "0x10"), "end"),
        (_mutated(("memory_map", "regions", 1, "end"), 0), r"regions\[1\]"),
        (_mutated(("memory_map", "regions", 0, "name"), 1), "name"),
        (_mutated(("memory_map", "unavailable_reason"), "stale"), "memory_map"),
        (_mutated(("memory_map", "source"), None), "memory_map.source"),
    ],
)
def test_malformed_snapshots_are_rejected(payload: object, message: str) -> None:
    """The agent never receives a partially trusted or ambiguous context."""
    with pytest.raises(TargetContextError, match=message):
        parse_target_context(payload)


def test_fetch_requests_target_context_through_the_real_client() -> None:
    """The snapshot travels through JSON-RPC with the collection timeout."""

    async def exercise() -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
        async with FakeRpcServer({"target.context": SNAPSHOT}) as server:
            client = JsonRpcClient(server.url)
            await client.connect()
            try:
                context = await fetch_target_context(client, timeout=4)
            finally:
                await client.close()
        return context, server.requests

    context, requests = asyncio.run(exercise())

    assert context.ocd.value == "openocd (openocd)"
    assert requests == [("target.context", {"timeout": 4})]


def test_fetch_propagates_server_errors_and_rejects_malformed_results() -> None:
    """Transport errors and schema errors are distinct failures."""

    async def exercise(response: Any) -> None:
        async with FakeRpcServer({"target.context": response}) as server:
            client = JsonRpcClient(server.url)
            await client.connect()
            try:
                await fetch_target_context(client)
            finally:
                await client.close()

    with pytest.raises(RpcError):
        asyncio.run(exercise(RpcFailure(-32000, "GDB is not running")))
    with pytest.raises(TargetContextError):
        asyncio.run(exercise({"schema_version": 1}))
