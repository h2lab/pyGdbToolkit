# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for pyGdbServer's network-independent protocol behavior."""

import asyncio
import base64
import json
from pathlib import Path
import shutil
import socket
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from pyGdbClient.app import PyGdbClientApp
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
    server.workspace = tmp_path
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


@pytest.mark.parametrize(
    "command",
    [
        "memmap",
        "memmap help",
        "memmap discover --vendor st --verify",
        "memmap show",
        "memmap bases xilinx",
        "memmap probe --known --max-reads 2",
        "memmap probe --range 0x18000000:0x18000004 --allow-unsafe --ignore-memory-map",
        'memmap report "remote directory/mapping.json"',
        "gdb memmap show",
    ],
)
def test_memmap_is_dispatched_like_other_toolkit_commands(tmp_path: Path, command: str) -> None:
    """All memory operations use the common CLI path without server-side parsing."""
    server, mi = _server(tmp_path)
    response, _ = asyncio.run(
        server.handle_rpc_message(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "command.execute",
                    "params": {"command": command, "timeout": 60},
                }
            )
        )
    )
    expected = command[4:] if command.startswith("gdb ") else command
    assert mi.calls == [("console", expected, 60)]
    assert response["result"]["output"] == [f"output:{expected}"]


def test_memmap_execution_rpcs_are_not_special_cases(tmp_path: Path) -> None:
    """No duplicate execution API can bypass the normal command registry."""
    server, mi = _server(tmp_path)
    for method in (
        "memmap.discover",
        "memmap.probe",
        "memmap.bases",
        "memmap.report",
        "memmap.status",
    ):
        response, _ = asyncio.run(
            server.handle_rpc_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": method,
                    }
                )
            )
        )
        assert response["error"]["code"] == -32601
    assert not mi.calls


def test_workspace_upload_and_listing_do_not_call_gdb(tmp_path: Path) -> None:
    """Binary uploads and default, relative, and absolute listings bypass MI."""
    server, fake_mi = _server(tmp_path)
    directory = tmp_path / "remote files"
    directory.mkdir()
    content = b"\x00\xfffirmware\n"

    async def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        response, _ = await server.handle_rpc_message(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        )
        assert response is not None
        assert "error" not in response
        return response["result"]

    async def exercise() -> None:
        for destination in (None, "remote files", str(directory)):
            params = {"filename": "firmware.bin", "content": base64.b64encode(content).decode()}
            if destination is not None:
                params["directory"] = destination
            result = await request("workspace.upload", params)
            assert Path(result["path"]).read_bytes() == content
            assert result["size"] == len(content)
        default = await request("workspace.list", {})
        assert default["path"] == str(tmp_path)
        assert {"name": "remote files", "is_directory": True} in default["entries"]
        for destination in ("remote files", str(directory)):
            listing = await request("workspace.list", {"directory": destination})
            assert listing == {
                "path": str(directory),
                "entries": [{"name": "firmware.bin", "is_directory": False}],
            }
        await request("workspace.upload", {"filename": "empty", "content": ""})
        assert (tmp_path / "empty").read_bytes() == b""
        await request("workspace.upload", {"filename": "firmware.bin", "content": ""})
        assert (tmp_path / "firmware.bin").read_bytes() == b""

    asyncio.run(exercise())
    assert fake_mi.calls == []


@pytest.mark.parametrize(
    "method, params, code",
    [
        ("workspace.upload", {"filename": "../escape", "content": ""}, -32602),
        ("workspace.upload", {"filename": ".", "content": ""}, -32602),
        ("workspace.upload", {"filename": "file", "content": "!"}, -32602),
        ("workspace.upload", {"filename": "file", "content": 42}, -32602),
        ("workspace.upload", {"filename": "file"}, -32602),
        ("workspace.list", {"directory": 42}, -32602),
        ("workspace.list", {"directory": "missing"}, -32000),
        ("workspace.upload", {"filename": "file", "content": "", "directory": "missing"}, -32000),
    ],
)
def test_workspace_errors_do_not_call_gdb(
    tmp_path: Path, method: str, params: dict[str, Any], code: int
) -> None:
    """Invalid file requests fail without dispatching a GDB command."""
    server, fake_mi = _server(tmp_path)
    response, _ = asyncio.run(
        server.handle_rpc_message(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        )
    )
    assert response is not None
    assert response["error"]["code"] == code
    assert fake_mi.calls == []


def test_workspace_upload_size_limit(tmp_path: Path) -> None:
    """The server accepts the size boundary and rejects larger uploads without MI."""
    server, fake_mi = _server(tmp_path)

    async def exercise() -> None:
        for size in (5 * 1024 * 1024, 5 * 1024 * 1024 + 1):
            response, _ = await server.handle_rpc_message(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "workspace.upload",
                        "params": {
                            "filename": "large.bin",
                            "content": base64.b64encode(bytes(size)).decode("ascii"),
                        },
                    }
                )
            )
            assert response is not None
            if size == 5 * 1024 * 1024:
                assert response["result"]["size"] == size
            else:
                assert response["error"]["code"] == -32602
        assert (tmp_path / "large.bin").stat().st_size == 5 * 1024 * 1024

    asyncio.run(exercise())
    assert fake_mi.calls == []


def test_client_workspace_commands_over_websocket_do_not_call_gdb(tmp_path: Path) -> None:
    """Actual client commands install and list files through the server transport."""
    server, fake_mi = _server(tmp_path)
    source_directory = tmp_path / "client"
    source_directory.mkdir()
    source = source_directory / "firmware image.bin"
    content = b"\x00\xfffirmware\n"
    source.write_bytes(content)
    remote_directory = tmp_path / "server"
    remote_directory.mkdir()
    server.workspace = remote_directory

    async def exercise() -> None:
        async with serve(server._handle_connection, "127.0.0.1", 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            app = PyGdbClientApp(f"ws://127.0.0.1:{port}")
            app._append_output = Mock()
            await app.client.connect()
            app._connected = True
            try:
                await app.execute_command(f'upload "{source}"')
                assert (remote_directory / source.name).read_bytes() == content
                await app.execute_command("ls")
                app._append_output.assert_any_call(f"  {source.name}")
                await app.execute_command(f'upload "{source}" "{remote_directory}"')
                await app.execute_command(f'ls "{remote_directory}"')
                await app.execute_command("ls missing")
                assert app._append_output.call_args.kwargs == {"error": True}
            finally:
                await app.client.close()

    asyncio.run(exercise())
    assert fake_mi.calls == []


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
    assert _ocd_listener_message("JLinkGDBServer", 43123) == "Connected to target"
    assert _ocd_listener_message("/opt/SEGGER/JLinkGDBServerCLExe", 43123) == (
        "Connected to target"
    )
    assert _ocd_listener_message("vendor-ocd", 43123) is None


def _jlink_startup_server(tmp_path: Path, messages: list[str]) -> PyGdbServer:
    server, _ = _server(tmp_path)
    server.config = SimpleNamespace(ocd_path="JLinkGDBServer", startup_timeout=0.01)
    server.ocd = SimpleNamespace(process=SimpleNamespace(returncode=None))
    server.gdb_port = 43123
    for message in messages:
        server.logs.append("ocd", "stdout", message)
    return server


def test_jlink_readiness_does_not_open_a_test_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the target-ready banner permits starting GDB, without a TCP probe."""
    probe = AsyncMock(side_effect=AssertionError("Unexpected TCP probe"))
    monkeypatch.setattr(asyncio, "open_connection", probe)
    server = _jlink_startup_server(
        tmp_path,
        [
            "Listening on TCP/IP port 43123",
            "Connected to target",
        ],
    )
    asyncio.run(server._wait_for_ocd())
    probe.assert_not_called()


def test_jlink_listening_port_is_not_target_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listener opens before J-Link has connected to the target CPU."""
    probe = AsyncMock(side_effect=AssertionError("Unexpected TCP probe"))
    monkeypatch.setattr(asyncio, "open_connection", probe)
    server = _jlink_startup_server(tmp_path, ["Listening on TCP/IP port 43123"])
    with pytest.raises(TimeoutError, match="did not become ready"):
        asyncio.run(server._wait_for_ocd())
    probe.assert_not_called()


@pytest.mark.parametrize("returncode", [None, 1])
def test_jlink_target_failure_reports_the_actual_startup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int | None
) -> None:
    """Reproduce the i.MX8MP failure rather than reporting a later GDB timeout."""
    probe = AsyncMock(side_effect=AssertionError("Unexpected TCP probe"))
    monkeypatch.setattr(asyncio, "open_connection", probe)
    server = _jlink_startup_server(
        tmp_path,
        [
            "Listening on TCP/IP port 43123",
            "WARNING: Identified core does not match configuration. "
            "(Found: Cortex-M0, Configured: Cortex-M7)",
            "ERROR: Failed to halt CPU.",
            "ERROR: Could not connect to target.",
        ],
    )
    server.ocd.process.returncode = returncode
    with pytest.raises(RuntimeError, match="J-Link startup failed") as error:
        asyncio.run(server._wait_for_ocd())
    assert "Found: Cortex-M0, Configured: Cortex-M7" in str(error.value)
    assert "Could not connect to target" in str(error.value)
    probe.assert_not_called()


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


def test_core_rpc_returns_structured_metadata(tmp_path: Path) -> None:
    """CLI and dedicated RPC select through the same GDB-side runtime."""
    core = {
        "id": 1,
        "name": "rp2350.cm1",
        "selected": True,
        "endpoint": "localhost:3333",
        "inferior": 1,
        "thread": 2,
    }

    class CoreMiSession(FakeMiSession):
        async def console(self, command: str, timeout: float = 30.0) -> MiResult:
            self.calls.append(("console", command, timeout))
            data = {"cores": [core]} if "CORES.list()" in command else {"core": core}
            return MiResult("done", "done", ("PYGDBSERVER_CORE_JSON:" + json.dumps(data),))

    async def exercise() -> None:
        server, _ = _server(tmp_path)
        server.mi = CoreMiSession()  # type: ignore[assignment]
        for method, params, key in (
            ("target.cores", {}, "cores"),
            ("target.core", {}, "core"),
            ("target.select_core", {"core": 1, "timeout": 7}, "core"),
        ):
            response, _ = await server.handle_rpc_message(
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            )
            assert response is not None
            assert response["result"][key] == ([core] if key == "cores" else core)
        calls = server.mi.calls
        assert "CORES.list()" in calls[0][1]
        assert "CORES.current()" in calls[1][1]
        assert "CORES.select(1)" in calls[2][1]
        assert calls[2][2] == 7

    asyncio.run(exercise())


@pytest.mark.parametrize("value", [-1, True, "1", 1.5, None])
def test_core_rpc_rejects_invalid_identifiers(tmp_path: Path, value: Any) -> None:
    """Invalid core identifiers never enter a GDB Python expression."""
    server, fake_mi = _server(tmp_path)
    response, _ = asyncio.run(
        server.handle_rpc_message(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "target.select_core",
                    "params": {"core": value},
                }
            )
        )
    )
    assert response is not None
    assert response["error"]["code"] == -32602
    assert not fake_mi.calls


def test_target_status_prefers_selected_physical_core(tmp_path: Path) -> None:
    """A core field on the first MI thread is not the selected physical CPU."""

    class CoreMiSession(FakeMiSession):
        async def execute(self, command: str, timeout: float = 30.0) -> MiResult:
            return MiResult(
                "done", 'done,threads=[{id="1",core="0",state="stopped"}],current-thread-id="2"', ()
            )

        async def console(self, command: str, timeout: float = 30.0) -> MiResult:
            return MiResult("done", "done", ('PYGDBSERVER_CORE_JSON:{"core":{"id":1}}',))

    server, _ = _server(tmp_path)
    server.mi = CoreMiSession()  # type: ignore[assignment]
    status = asyncio.run(server.target_status())
    assert status["core"] == "1"
    assert status["thread_id"] == "2"


def test_toolkit_commands_and_help_come_from_the_gdb_session(tmp_path: Path) -> None:
    """Toolkit command listing and help are read from the GDB-side session registry."""
    helps = [
        {"name": "lscpu", "summary": "Identify the core.", "usage": [], "notes": []},
        {
            "name": "svd",
            "summary": "Inspect registers.",
            "usage": [{"syntax": "svd load", "description": "Load the SVD"}],
            "notes": [],
        },
        {
            "name": "memmap",
            "summary": "Discover memory regions.",
            "usage": [{"syntax": "memmap discover --verify", "description": "Verify endpoints"}],
            "notes": ["Candidates are not physical capacities."],
        },
    ]

    class HelpMiSession(FakeMiSession):
        async def console(self, command: str, timeout: float = 30.0) -> MiResult:
            self.calls.append(("console", command, timeout))
            return MiResult("done", "done", ("PYGDBSERVER_TOOLKIT_HELP_JSON:" + json.dumps(helps),))

    async def exercise() -> list[dict[str, Any]]:
        server, _ = _server(tmp_path)
        server.mi = HelpMiSession()  # type: ignore[assignment]
        responses = []
        for identifier, method, params in (
            (1, "toolkit.commands", {}),
            (2, "toolkit.help", {}),
            (3, "toolkit.help", {"command": "svd"}),
            (4, "toolkit.help", {"command": "unknown"}),
            (5, "toolkit.help", {"command": "memmap"}),
        ):
            response, _ = await server.handle_rpc_message(
                json.dumps({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params})
            )
            assert response is not None
            responses.append(response)
        return responses

    listing, all_help, svd_help, unknown, memmap_help = asyncio.run(exercise())

    assert listing["result"]["commands"] == [
        {"name": "lscpu", "summary": "Identify the core."},
        {"name": "svd", "summary": "Inspect registers."},
        {"name": "memmap", "summary": "Discover memory regions."},
    ]
    assert all_help["result"]["commands"] == helps
    assert svd_help["result"]["commands"] == [helps[1]]
    assert unknown["error"]["code"] == -32602
    assert memmap_help["result"]["commands"] == [helps[2]]


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
