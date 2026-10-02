# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for pyGdbServer's network-independent protocol behavior."""

import asyncio
import json
from pathlib import Path
import shutil
import socket
from typing import Any

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from pyGdbServer.logs import LogStore
from pyGdbServer.mi import MiResult, MiSession, _decode_mi_string
from pyGdbServer.server import PyGdbServer, _free_loopback_ports, _ocd_listener_message


def test_gdb_and_telnet_ports_are_distinct_and_available() -> None:
    """The operating system allocates two distinct free loopback ports."""
    gdb_port, telnet_port = _free_loopback_ports()

    assert 0 < gdb_port <= 65535
    assert 0 < telnet_port <= 65535
    assert gdb_port != telnet_port
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as gdb_socket,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as telnet_socket,
    ):
        gdb_socket.bind(("127.0.0.1", gdb_port))
        telnet_socket.bind(("127.0.0.1", telnet_port))


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


def test_mi_publishes_console_output_after_async_continue_stops(tmp_path: Path) -> None:
    """Breakpoint command output remains available after MI reports continue running."""

    async def exercise() -> list[dict[str, int | str]]:
        logs = LogStore(tmp_path)
        session = MiSession("unused-gdb", (), logs)
        reader = asyncio.StreamReader()
        reader.feed_data(
            b"1^running\n"
            b'*running,thread-id="all"\n'
            b'~"scheduler trace result\\n"\n'
            b'*stopped,reason="breakpoint-hit",thread-id="1"\n'
            b'~"late breakpoint callback output\\n"\n'
        )
        reader.feed_eof()
        await session._read_stdout(reader)
        return [event for event in logs.get() if event["stream"] == "console"]

    output_events = asyncio.run(exercise())

    assert [event["message"] for event in output_events] == [
        "scheduler trace result",
        "late breakpoint callback output",
    ]


def test_known_ocds_use_passive_listener_readiness() -> None:
    """Known OCDs are recognized by their listener logs, without TCP probes."""
    assert _ocd_listener_message("pyocd", 43123) == "GDB server listening on port 43123"
    assert _ocd_listener_message("openocd", 43123) == "Listening on port 43123 for gdb connections"
    assert _ocd_listener_message("vendor-ocd", 43123) is None


def test_target_status_reports_thread_state_and_discovered_access_port(tmp_path: Path) -> None:
    """The target dashboard gets state and Access Port data from structured sources."""

    class TargetMiSession(FakeMiSession):
        async def execute(self, command: str, timeout: float = 30.0) -> MiResult:
            self.calls.append(("mi", command, timeout))
            return MiResult(
                "done",
                'done,threads=[{id="1",state="stopped"}],current-thread-id="1"',
                (),
            )

        async def console(self, command: str, timeout: float = 30.0) -> MiResult:
            self.calls.append(("console", command, timeout))
            return MiResult("done", "done", ("Core 0 (Cortex-M33) is selected\n",))

    server = object.__new__(PyGdbServer)
    server.logs = LogStore(tmp_path)
    server.mi = TargetMiSession()  # type: ignore[assignment]
    server.logs.append("ocd", "stderr", "AHB5-AP#0 IDR = 0x14770015")

    status = asyncio.run(server.target_status())

    assert status == {
        "state": "stopped",
        "thread_id": "1",
        "core": "0",
        "access_port": "AHB5-AP#0",
        "access_ports": ["AHB5-AP#0"],
    }


def test_svd_peripherals_returns_structured_device_metadata(tmp_path: Path) -> None:
    """The SVD tree endpoint returns a JSON payload independent of Rich tables."""

    class SvdMiSession(FakeMiSession):
        async def console(self, command: str, timeout: float = 30.0) -> MiResult:
            self.calls.append(("console", command, timeout))
            data = {
                "device": "TestDevice",
                "peripherals": [
                    {
                        "name": "GPIOA",
                        "description": "GPIO port A",
                        "base_address": 0x48000000,
                        "registers": [],
                    }
                ],
            }
            return MiResult(
                "done",
                "done",
                ("PYGDBSERVER_SVD_JSON:" + json.dumps(data),),
            )

    server = object.__new__(PyGdbServer)
    server.mi = SvdMiSession()  # type: ignore[assignment]

    result = asyncio.run(server.svd_peripherals())

    assert result["loaded"] is True
    assert result["device"] == "TestDevice"
    assert result["peripherals"][0]["name"] == "GPIOA"


def test_shutdown_acknowledges_before_stopping_gdb_and_ocd(tmp_path: Path) -> None:
    """Shutdown sends its JSON-RPC result before stopping both managed processes."""

    class ManagedStop:
        def __init__(self, name: str, stopped: list[str]) -> None:
            self.name = name
            self.stopped = stopped

        async def stop(self) -> None:
            self.stopped.append(self.name)

    async def exercise() -> tuple[dict[str, Any], list[str]]:
        server = object.__new__(PyGdbServer)
        server.logs = LogStore(tmp_path)
        server._shutdown = asyncio.Event()
        server._websocket_server = None
        stopped: list[str] = []
        server.mi = ManagedStop("gdb", stopped)  # type: ignore[assignment]
        server.ocd = ManagedStop("ocd", stopped)  # type: ignore[assignment]

        async with serve(server._handle_connection, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}") as websocket:
                await websocket.send(
                    json.dumps({"jsonrpc": "2.0", "id": 1, "method": "server.shutdown"})
                )
                response = json.loads(await asyncio.wait_for(websocket.recv(), 1))
                await asyncio.wait_for(server._shutdown.wait(), 1)
        await server.stop()
        return response, stopped

    response, stopped = asyncio.run(exercise())

    assert response["result"] == {"stopping": True}
    assert stopped == ["gdb", "ocd"]


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
