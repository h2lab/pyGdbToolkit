# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Client transport and Textual dashboard tests."""

import asyncio
import base64
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from typing import Any

import pytest
from textual.widgets import Input, Select, Static, Tree
from websockets.asyncio.server import ServerConnection, serve

from pyGdbClient.app import PyGdbClientApp, _help_text
from pyGdbClient.rpc import JsonRpcClient, RpcError

_TOOLKIT_HELP: list[dict[str, Any]] = [
    {
        "name": "lscpu",
        "summary": "Identify the core.",
        "usage": [{"syntax": "lscpu", "description": "Display the CPU report"}],
        "notes": [],
    },
    {
        "name": "rtos",
        "summary": "Manage the RTOS.",
        "usage": [{"syntax": "rtos showsched <num>", "description": "Trace elections"}],
        "notes": ["<path> is resolved by GDB."],
    },
    {
        "name": "memmap",
        "summary": "Discover memory regions.",
        "usage": [
            {"syntax": "memmap discover --verify", "description": "Verify declared endpoints"}
        ],
        "notes": ["Candidates are not physical capacities."],
    },
]


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
            "ocd": {"gdb_port": 34567, "executable": "pyocd"},
        }
    elif method == "target.status":
        params = {"state": "stopped", "thread_id": "1", "core": "0", "access_port": "AP#0"}
    elif method == "target.cores":
        params = {
            "cores": [
                {"id": 0, "name": "Cortex-M33", "selected": True, "endpoint": "localhost:3333"},
                {"id": 1, "name": "Cortex-M33", "selected": False, "endpoint": "localhost:3334"},
            ]
        }
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
    elif method == "toolkit.help":
        params = {"commands": _TOOLKIT_HELP}
    elif method == "missing":
        error = {"code": -32601, "message": "Method not found"}
    elif method == "logs.subscribe":
        params = {"subscribed": True}
    elif method == "server.shutdown":
        params = {"stopping": True}

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
            await websocket.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "log.event",
                        "params": {
                            "source": "gdb",
                            "stream": "console",
                            "message": "scheduler trace result",
                        },
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


def test_async_gdb_console_notification_is_shown_in_command_output() -> None:
    """Unsolicited GDB console output is visible in the dashboard result pane."""

    async def exercise() -> str:
        async def handler(websocket: ServerConnection) -> None:
            async for payload in websocket:
                await _respond(websocket, json.loads(payload))

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            app = PyGdbClientApp(f"ws://127.0.0.1:{port}")
            async with app.run_test(size=(150, 48)) as pilot:
                await pilot.pause(0.5)
                await pilot.pause(0.2)
                output = app.query_one("#command-output")
                return "\n".join(str(line) for line in output.lines)

    rendered_output = asyncio.run(exercise())

    assert "scheduler trace result" in rendered_output


def test_command_history_up_down_and_local_listing() -> None:
    """Up/Down browse submitted commands and history does not reach GDB."""

    async def exercise() -> tuple[list[str], str, str]:
        remote_commands: list[str] = []

        async def handler(websocket: ServerConnection) -> None:
            async for payload in websocket:
                request = json.loads(payload)
                if request["method"] == "command.execute":
                    remote_commands.append(request["params"]["command"])
                await _respond(websocket, request)

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            app = PyGdbClientApp(f"ws://127.0.0.1:{port}")
            async with app.run_test(size=(150, 48)) as pilot:
                await pilot.pause(0.5)
                command_input = app.query_one("#command", Input)
                for command in ("gdb first", "gdb second"):
                    command_input.value = command
                    await pilot.press("enter")
                    await pilot.pause(0.1)

                command_input.value = "draft"
                await pilot.press("up")
                assert command_input.value == "gdb second"
                await pilot.press("up")
                assert command_input.value == "gdb first"
                await pilot.press("down")
                assert command_input.value == "gdb second"
                await pilot.press("down")
                assert command_input.value == "draft"

                command_input.value = "history"
                await pilot.press("enter")
                await pilot.pause(0.1)
                rendered = "\n".join(str(line) for line in app.query_one("#command-output").lines)
                return remote_commands, rendered, command_input.value

    remote_commands, rendered, input_value = asyncio.run(exercise())

    assert "gdb first" in remote_commands
    assert "gdb second" in remote_commands
    assert "history" not in remote_commands
    assert "Command history (3 / 40)" in rendered
    assert "gdb first" in rendered
    assert "gdb second" in rendered
    assert input_value == ""


def test_command_history_keeps_only_the_latest_40_entries() -> None:
    """Old submitted commands are discarded when the history reaches its limit."""
    app = PyGdbClientApp("ws://127.0.0.1:1234")
    for index in range(45):
        app._remember_command(f"command-{index}")

    assert len(app._command_history) == 40
    assert app._command_history[0] == "command-5"
    assert app._command_history[-1] == "command-44"


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


def test_quit_commands_only_stop_the_server_when_all_is_requested() -> None:
    """Quit closes only the dashboard; quit --all also sends server.shutdown."""

    async def exercise(command: str) -> list[str]:
        methods: list[str] = []

        async def handler(websocket: ServerConnection) -> None:
            async for payload in websocket:
                request = json.loads(payload)
                methods.append(request["method"])
                await _respond(websocket, request)

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            app = PyGdbClientApp(f"ws://127.0.0.1:{port}")
            async with app.run_test(size=(150, 48)) as pilot:
                await pilot.pause(0.4)
                await pilot.click("#command")
                await pilot.press(*list(command))
                await pilot.press("enter")
                await pilot.pause(0.2)
        return methods

    local_methods = asyncio.run(exercise("quit"))
    all_methods = asyncio.run(exercise("quit --all"))

    assert "server.shutdown" not in local_methods
    assert "server.shutdown" in all_methods


def test_help_lists_client_toolkit_gdb_and_selected_ocd_manual() -> None:
    """Help includes client commands, server-provided toolkit help, and OCD manuals."""
    pyocd_help = "\n".join(paragraph.plain for paragraph in _help_text("pyocd", _TOOLKIT_HELP))
    openocd_help = "\n".join(paragraph.plain for paragraph in _help_text("openocd"))

    for expected in (
        "help",
        "history",
        "upload FILE [DIR]",
        "ls [DIR]",
        "quit --all",
        "lscpu",
        "Display the CPU report",
        "rtos showsched <num>",
        "memmap discover --verify",
        "Candidates are not physical capacities.",
        "<path> is resolved by GDB.",
        "gdb XXX",
        "monitor XXX",
        "https://sourceware.org/gdb/current/onlinedocs/gdb.html/",
    ):
        assert expected in pyocd_help
    assert "https://pyocd.io/docs/" in pyocd_help
    assert "https://openocd.org/doc/html/" in openocd_help
    assert "not connected" in openocd_help


@pytest.mark.parametrize("directory", [None, "/tmp", "remote directory"])
def test_client_upload_uses_a_dedicated_rpc(tmp_path: Path, directory: str | None) -> None:
    """Quoted local and remote paths transfer binary data without any GDB RPC."""
    source = tmp_path / "firmware image.bin"
    content = b"\x00\xffbinary\n"
    source.write_bytes(content)
    app = PyGdbClientApp("ws://127.0.0.1:1234")
    app._connected = True
    app.client.request = AsyncMock(
        return_value={"path": "/tmp/firmware image.bin", "size": len(content)}
    )
    app._append_output = Mock()
    command = f'upload "{source}"'
    if directory is not None:
        command += f' "{directory}"'

    asyncio.run(app.execute_command(command))

    params = {"filename": source.name, "content": base64.b64encode(content).decode("ascii")}
    if directory is not None:
        params["directory"] = directory
    app.client.request.assert_awaited_once_with("workspace.upload", params, timeout=300)
    app._append_output.assert_called_with("Uploaded /tmp/firmware image.bin (9 bytes)")


@pytest.mark.parametrize(
    "command, params", [("ls", {}), ('ls "/tmp/remote files"', {"directory": "/tmp/remote files"})]
)
def test_client_ls_uses_a_dedicated_rpc(command: str, params: dict[str, Any]) -> None:
    """Remote listings render entries and never request a target refresh."""
    app = PyGdbClientApp("ws://127.0.0.1:1234")
    app._connected = True
    app.client.request = AsyncMock(
        return_value={
            "path": "/workspace",
            "entries": [
                {"name": "firmware.bin", "is_directory": False},
                {"name": "images", "is_directory": True},
            ],
        }
    )
    app._append_output = Mock()

    asyncio.run(app.execute_command(command))

    app.client.request.assert_awaited_once_with("workspace.list", params)
    app._append_output.assert_any_call("/workspace")
    app._append_output.assert_any_call("  firmware.bin")
    app._append_output.assert_any_call("  images/")


@pytest.mark.parametrize(
    "command", ["upload", "upload a b c", "upload /nonexistent/file", "ls a b", 'ls "unfinished']
)
def test_invalid_workspace_commands_never_reach_gdb(command: str) -> None:
    """Syntax and local file errors stay in the client output panel."""
    app = PyGdbClientApp("ws://127.0.0.1:1234")
    app._connected = True
    app.client.request = AsyncMock()
    app._append_output = Mock()

    asyncio.run(app.execute_command(command))

    app.client.request.assert_not_awaited()
    assert app._append_output.call_args.kwargs == {"error": True}


def test_client_rejects_oversized_upload_before_sending(tmp_path: Path) -> None:
    """Oversized files fail locally instead of exceeding the WebSocket limit."""
    source = tmp_path / "large.bin"
    with source.open("wb") as output:
        output.truncate(5 * 1024 * 1024 + 1)
    app = PyGdbClientApp("ws://127.0.0.1:1234")
    app._connected = True
    app.client.request = AsyncMock()
    app._append_output = Mock()

    asyncio.run(app.execute_command(f"upload {source}"))

    app.client.request.assert_not_awaited()
    app._append_output.assert_called_with("upload size must not exceed 5 MiB", error=True)


@pytest.mark.parametrize(
    "command",
    [
        "memmap",
        "memmap help",
        "memmap discover",
        "memmap discover --vendor st --verify",
        "memmap show",
        "memmap bases nxp",
        "memmap probe --known --max-reads 2",
        "memmap probe --range 0x18000000:0x18000004 --allow-unsafe --ignore-memory-map",
        'memmap report "remote directory/mapping.json"',
        "gdb memmap show",
        "memmap unsupported-option",
    ],
)
def test_memmap_uses_the_common_command_rpc(command: str) -> None:
    """Forward all memory commands verbatim; syntax validation stays in GDB."""
    app = PyGdbClientApp("ws://127.0.0.1:1234")
    app._connected = True
    app.client.request = AsyncMock(return_value={"class": "done", "output": ["memory evidence"]})
    app._append_output = Mock()
    app._append_rendered_output = Mock()

    asyncio.run(app.execute_command(command))

    app.client.request.assert_awaited_once_with(
        "command.execute", {"command": command}, timeout=300
    )
    app._append_rendered_output.assert_called_once_with(["memory evidence"])


def test_help_command_is_handled_locally() -> None:
    """Submitting help fetches toolkit help over RPC instead of running a GDB command."""

    async def exercise() -> tuple[list[str], str]:
        methods: list[str] = []

        async def handler(websocket: ServerConnection) -> None:
            async for payload in websocket:
                request = json.loads(payload)
                methods.append(request["method"])
                await _respond(websocket, request)

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            app = PyGdbClientApp(f"ws://127.0.0.1:{port}")
            async with app.run_test(size=(150, 48)) as pilot:
                await pilot.pause(0.5)
                command_count = methods.count("command.execute")
                await pilot.click("#command")
                await pilot.press(*list("help"))
                await pilot.press("enter")
                await pilot.pause(0.2)
                assert methods.count("command.execute") == command_count
                rendered = "\n".join(str(line) for line in app.query_one("#command-output").lines)
        return methods, rendered

    methods, rendered = asyncio.run(exercise())

    assert "toolkit.help" in methods
    assert "rtos showsched <num>" in rendered


@pytest.mark.parametrize("size", [(150, 48), (80, 40)])
def test_core_panel_selection_and_cli_refresh(size) -> None:
    """The left panel follows confirmed RPC and console core selections."""

    async def exercise():
        active = 0
        selections = []

        async def handler(websocket):
            nonlocal active
            async for payload in websocket:
                request = json.loads(payload)
                method = request["method"]
                if method == "target.select_core":
                    active = request["params"]["core"]
                    selections.append(active)
                    result = {"core": {"id": active, "name": f"rp2350.cm{active}"}}
                elif method == "target.cores":
                    result = {
                        "cores": [
                            {
                                "id": index,
                                "name": f"rp2350.cm{index}",
                                "selected": index == active,
                                "endpoint": "localhost:3333",
                            }
                            for index in (0, 1)
                        ]
                    }
                else:
                    if method == "command.execute" and request["params"]["command"] == "dap core 0":
                        active = 0
                    await _respond(websocket, request)
                    continue
                await websocket.send(
                    json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})
                )

        async with serve(handler, "127.0.0.1", 0) as server:
            app = PyGdbClientApp(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
            async with app.run_test(size=size) as pilot:
                await pilot.pause(0.5)
                selector = app.query_one("#core-select", Select)
                assert selector.parent.id == "logs-column"
                assert selector.value == 0
                assert selections == []
                assert "Active core: 0" in str(app.query_one("#core-current", Static).render())
                await pilot.click("#core-select")
                await pilot.press("end", "enter")
                await pilot.pause(0.3)
                assert selections == [1]
                assert selector.value == 1
                assert "Active core: 1" in str(app.query_one("#core-current", Static).render())
                await app.execute_command("dap core 0")
                await pilot.pause(0.1)
                assert selector.value == 0
                assert selections == [1]
                assert "Active core: 0" in str(app.query_one("#core-current", Static).render())

    asyncio.run(exercise())


def test_core_selection_failure_restores_confirmed_core() -> None:
    """A refused selection keeps the previous core visible and usable."""

    async def exercise():
        async def handler(websocket):
            async for payload in websocket:
                request = json.loads(payload)
                if request["method"] == "target.select_core":
                    await websocket.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": request["id"],
                                "error": {"code": -32000, "message": "connection refused"},
                            }
                        )
                    )
                else:
                    await _respond(websocket, request)

        async with serve(handler, "127.0.0.1", 0) as server:
            app = PyGdbClientApp(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
            async with app.run_test(size=(150, 48)) as pilot:
                await pilot.pause(0.5)
                selector = app.query_one("#core-select", Select)
                selector.value = 1
                await pilot.pause(0.3)
                assert selector.value == 0
                assert not selector.disabled
                assert "Active core: 0" in str(app.query_one("#core-current", Static).render())
                rendered = "\n".join(str(line) for line in app.query_one("#command-output").lines)
                assert "Core selection failed" in rendered
                assert "connection refused" in rendered

    asyncio.run(exercise())


def test_core_inventory_unavailable_disables_selection() -> None:
    """Servers without a usable inventory do not offer a guessed CPU choice."""

    async def exercise():
        async def handler(websocket):
            async for payload in websocket:
                request = json.loads(payload)
                if request["method"] == "target.cores":
                    await websocket.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": request["id"],
                                "error": {"code": -32601, "message": "Method not found"},
                            }
                        )
                    )
                else:
                    await _respond(websocket, request)

        async with serve(handler, "127.0.0.1", 0) as server:
            app = PyGdbClientApp(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
            async with app.run_test(size=(150, 48)) as pilot:
                await pilot.pause(0.5)
                assert app.query_one("#core-select", Select).disabled
                assert "unavailable" in str(app.query_one("#core-current", Static).render())

    asyncio.run(exercise())
