# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Opt-in RP2350 checks using the example board configurations and real OCDs."""

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest
import rich
from websockets.asyncio.client import connect

from pyGdbServer.config import load_config
from pyGdbServer.process import ManagedProcess
from pyGdbServer.server import PyGdbServer, _free_loopback_ports

pytestmark = pytest.mark.skipif(
    os.environ.get("PYGDB_CORE_HARDWARE") != "1",
    reason="requires the RP2350 probe; set PYGDB_CORE_HARDWARE=1",
)
ROOT = Path(__file__).resolve().parents[1]


def board_config(backend, tmp_path):
    name = "pico2w.json" if backend == "pyocd" else "pico2w-openocd.json"
    site_packages = str(Path(rich.__file__).resolve().parents[1])
    return replace(
        load_config(ROOT / "doc" / "examples" / "boards" / name),
        listen_host="127.0.0.1",
        listen_port=0,
        log_directory=tmp_path,
        gdb_args=("-ex", f"python import sys; sys.path.insert(0, {site_packages!r})"),
        gdb_init=("set arch arm", "set mem inaccessible-by-default off"),
    )


@pytest.mark.parametrize("backend", ["openocd", "pyocd"])
@pytest.mark.parametrize("connection_type", ["remote", "extended-remote"])
def test_standalone_gdb_core_selection(backend, connection_type, tmp_path):
    """An independently launched OCD supports repeated GDB CPU selection."""

    async def exercise():
        config = board_config(backend, tmp_path)
        server = PyGdbServer(config)
        server.gdb_port, server.telnet_port = _free_loopback_ports()
        server.ocd = ManagedProcess(
            "ocd", config.ocd_command(server.gdb_port, server.telnet_port), server.logs
        )
        try:
            await server.ocd.start()
            await server._wait_for_ocd()
            commands = [
                "set confirm off",
                f"target {connection_type} 127.0.0.1:{server.gdb_port}",
                f"python import sys; sys.path.insert(0, {str(ROOT / 'src')!r}); import pyGdbToolkit",
                "dap core list",
                "python from pyGdbToolkit.core_runtime import CORES; assert CORES.current().id == 0",
            ]
            for core_id in (1, 0, 1, 0):
                commands.extend(
                    [
                        f"dap core {core_id}",
                        f"python assert CORES.current().id == {core_id}",
                        "info registers pc",
                    ]
                )
                if backend == "pyocd":
                    commands.append(
                        f"python import gdb; assert gdb.selected_inferior().connection.details.endswith(':{server.gdb_port + core_id}')"
                    )
                else:
                    commands.append(
                        f"python import gdb; assert gdb.selected_thread().name == 'rp2350.cm{core_id}'"
                    )
            if backend == "pyocd":
                commands.append("python assert len(gdb.inferiors()) == 2")
            commands.extend(
                [
                    "dap list",
                    "python print('CORE_HARDWARE_OK')",
                ]
            )
            arguments = [config.gdb_path, "-q", "-nx", "-batch", *config.gdb_args]
            for command in commands:
                arguments.extend(["-ex", command])
            process = await asyncio.create_subprocess_exec(
                *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), 60)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            output = stdout.decode()
            assert process.returncode == 0, output
            assert "CORE_HARDWARE_OK" in output, output
            assert "Traceback" not in output, output
        finally:
            await server.stop()

    asyncio.run(exercise())


@pytest.mark.parametrize("backend", ["openocd", "pyocd"])
def test_websocket_rpc_core_selection(backend, tmp_path):
    """Real WebSocket RPCs and console commands share the selected GDB CPU."""

    async def exercise():
        server = PyGdbServer(board_config(backend, tmp_path))
        try:
            await server.start()
            async with connect(f"ws://127.0.0.1:{server.api_port}") as websocket:

                async def rpc(method, params=None):
                    await websocket.send(
                        json.dumps(
                            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
                        )
                    )
                    response = json.loads(await asyncio.wait_for(websocket.recv(), 40))
                    assert "error" not in response, response
                    return response["result"]

                cores = (await rpc("target.cores"))["cores"]
                assert [core["id"] for core in cores] == [0, 1]
                for core_id in (1, 0, 1, 0):
                    selected = (await rpc("target.select_core", {"core": core_id}))["core"]
                    assert selected["id"] == core_id
                    assert selected["selected"] is True
                    if backend == "pyocd":
                        assert selected["endpoint"].endswith(f":{server.gdb_port + core_id}")
                    else:
                        assert selected["name"] == f"rp2350.cm{core_id}"
                    assert (await rpc("target.core"))["core"]["id"] == core_id
                    assert (await rpc("target.status"))["core"] == str(core_id)
                    registers = await rpc("command.execute", {"command": "info registers pc"})
                    assert "pc" in "".join(registers["output"])
                await rpc("command.execute", {"command": "dap core 1"})
                assert (await rpc("target.core"))["core"]["id"] == 1
                await rpc("command.execute", {"command": "dap core list"})
                await rpc("command.execute", {"command": "dap list"})
                if backend == "pyocd":
                    assert (
                        len({core["inferior"] for core in (await rpc("target.cores"))["cores"]})
                        == 2
                    )
        finally:
            await server.stop()

    asyncio.run(exercise())
