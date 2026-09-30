# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for pyGdbServer's network-independent protocol behavior."""

import asyncio
import json
from pathlib import Path
import shutil
from typing import Any

import pytest

from pyGdbServer.logs import LogStore
from pyGdbServer.mi import MiResult, MiSession, _decode_mi_string
from pyGdbServer.server import PyGdbServer, _ocd_listener_message


class FakeMiSession:
    """Record console and raw MI calls made by the RPC dispatcher."""

    def __init__(self) -> None:
        """Initialize an empty call history."""
        self.calls: list[tuple[str, str, float]] = []

    async def console(self, command: str, timeout: float) -> MiResult:
        """Record a console command and return a successful result."""
        self.calls.append(("console", command, timeout))
        return MiResult("done", "done", (f"output:{command}",))

    async def execute(self, command: str, timeout: float) -> MiResult:
        """Record a raw MI command and return a successful result."""
        self.calls.append(("mi", command, timeout))
        return MiResult("done", "done", ())


def _server(tmp_path: Path) -> tuple[PyGdbServer, FakeMiSession]:
    server = object.__new__(PyGdbServer)
    server.logs = LogStore(tmp_path)
    fake_mi = FakeMiSession()
    server.mi = fake_mi  # type: ignore[assignment]
    server._shutdown = asyncio.Event()
    return server, fake_mi


def test_command_prefixes_and_raw_mi_are_dispatched(tmp_path: Path) -> None:
    """Toolkit, GDB, monitor, and raw MI requests use their intended paths."""

    async def exercise() -> tuple[list[tuple[str, str, float]], list[dict[str, Any]]]:
        server, fake_mi = _server(tmp_path)
        responses = []
        for identifier, method, command in (
            (1, "command.execute", "lscpu"),
            (2, "command.execute", "gdb info registers"),
            (3, "command.execute", "monitor reset halt"),
            (4, "mi.execute", "-data-list-register-names"),
        ):
            response, _ = await server.handle_rpc_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": identifier,
                        "method": method,
                        "params": {"command": command},
                    }
                )
            )
            assert response is not None
            responses.append(response)
        return fake_mi.calls, responses

    calls, responses = asyncio.run(exercise())

    assert calls == [
        ("console", "lscpu", 30.0),
        ("console", "info registers", 30.0),
        ("console", "monitor reset halt", 30.0),
        ("mi", "-data-list-register-names", 30.0),
    ]
    assert [response["result"]["class"] for response in responses] == ["done"] * 4


def test_json_rpc_errors_and_notification(tmp_path: Path) -> None:
    """Malformed input, unknown methods, and notifications follow JSON-RPC 2.0."""

    async def exercise() -> tuple[dict[str, Any], dict[str, Any], object]:
        server, _ = _server(tmp_path)
        parse_error, _ = await server.handle_rpc_message("{")
        method_error, _ = await server.handle_rpc_message(
            '{"jsonrpc":"2.0","id":7,"method":"missing"}'
        )
        notification, _ = await server.handle_rpc_message(
            '{"jsonrpc":"2.0","method":"logs.subscribe"}'
        )
        assert parse_error is not None
        assert method_error is not None
        return parse_error, method_error, notification

    parse_error, method_error, notification = asyncio.run(exercise())

    assert parse_error["error"]["code"] == -32700
    assert method_error["error"]["code"] == -32601
    assert notification is None


def test_invalid_json_rpc_notification_has_no_response(tmp_path: Path) -> None:
    """Valid notifications never receive a response, including dispatch errors."""

    async def exercise() -> object:
        server, _ = _server(tmp_path)
        response, _ = await server.handle_rpc_message('{"jsonrpc":"2.0","method":"missing"}')
        return response

    assert asyncio.run(exercise()) is None


def test_mi_c_string_decodes_terminal_escape_and_utf8() -> None:
    """MI C escapes are decoded without corrupting literal Unicode text."""
    assert _decode_mi_string('"\\e[31mSTM32 µC\\n\\342\\224\\201"') == ("\x1b[31mSTM32 µC\n━")


def test_logs_are_ordered_retrievable_and_persistent(tmp_path: Path) -> None:
    """The in-memory log view and JSONL history contain the same events."""
    logs = LogStore(tmp_path)
    logs.append("ocd", "stdout", "ready\n")
    logs.append("gdb", "mi", "(gdb)\n")

    assert [event["sequence"] for event in logs.get(since=1)] == [2]
    records = [json.loads(line) for line in logs.path.read_text(encoding="utf-8").splitlines()]
    assert [record["message"] for record in records] == ["ready", "(gdb)"]


def test_known_ocds_use_passive_listener_readiness() -> None:
    """Known OCDs are recognized by their listener logs, without TCP probes."""
    assert _ocd_listener_message("pyocd", 43123) == "GDB server listening on port 43123"
    assert _ocd_listener_message("openocd", 43123) == "Listening on port 43123 for gdb connections"
    assert _ocd_listener_message("vendor-ocd", 43123) is None


@pytest.mark.skipif(shutil.which("gdb-multiarch") is None, reason="gdb-multiarch is unavailable")
def test_real_gdb_mi_handshake_and_command(tmp_path: Path) -> None:
    """The MI reader recognizes GDB's prompt and routes a tokenized command."""

    async def exercise() -> tuple[str, str]:
        logs = LogStore(tmp_path)
        session = MiSession("gdb-multiarch", (), logs)
        await session.start(timeout=5)
        try:
            result = await session.execute("-gdb-version")
            return session.interpreter, result.result_class
        finally:
            await session.stop()

    interpreter, result_class = asyncio.run(exercise())

    assert interpreter == "mi3"
    assert result_class == "done"
