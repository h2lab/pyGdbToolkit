# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Opt-in functional checks using real QEMU, GDB and WebSocket connections."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
from typing import Any

import pytest
import rich
from websockets.asyncio.client import ClientConnection, connect

from pyGdbServer.config import ServerConfig, load_config
from pyGdbServer.server import PyGdbServer

pytestmark = pytest.mark.skipif(
    os.environ.get("PYGDB_QEMU_TESTS") != "1",
    reason="requires real QEMU and Python-enabled GDB; run tox -e qemu",
)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def tools() -> tuple[str, str]:
    """Require working external tools instead of silently skipping requested CI tests."""
    qemu = shutil.which(os.environ.get("PYGDB_QEMU", "qemu-system-arm"))
    gdb = shutil.which(os.environ.get("PYGDB_QEMU_GDB", "gdb-multiarch"))
    if qemu is None or gdb is None:
        pytest.fail("QEMU tests require qemu-system-arm and gdb-multiarch on PATH")
    result = subprocess.run(
        [gdb, "-nx", "-batch", "-ex", "python import sys; assert sys.version_info >= (3, 12)"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, f"GDB requires embedded Python 3.12+: {result.stderr}"
    return qemu, gdb


def _configuration(
    tmp_path: Path,
    tools: tuple[str, str],
    machine: str = "netduinoplus2",
    *,
    explicit_endpoint: bool = True,
) -> ServerConfig:
    firmware = tmp_path / "minimal.bin"
    firmware.write_bytes(struct.pack("<IIHHH", 0x20001000, 0x08000009, 0x202A, 0x3001, 0xE7FE))
    arguments = (
        "-M",
        machine,
        "-kernel",
        str(firmware),
        "-display",
        "none",
        "-serial",
        "none",
        "-monitor",
        "none",
    )
    if explicit_endpoint:
        arguments += ("-S", "-gdb", "tcp:{loopback}:{gdb_port}")
    return replace(
        load_config(ROOT / "doc/examples/servercfg-qemu.json"),
        ocd_path=tools[0],
        gdb_path=tools[1],
        gdb_args=(
            "-ex",
            f"python import sys; sys.path.insert(0, {str(Path(rich.__file__).parents[1])!r})",
        ),
        ocd_args=arguments,
        listen_port=0,
        gdb_init=("set mem inaccessible-by-default off",),
        log_directory=tmp_path / "logs",
    )


@asynccontextmanager
async def _running(config: ServerConfig) -> AsyncIterator[PyGdbServer]:
    server = PyGdbServer(config)
    try:
        await server.start()
        yield server
    finally:
        await server.stop()
    assert server.ocd is not None and server.ocd.process is not None
    assert server.ocd.process.returncode is not None
    assert server.mi.process is not None and server.mi.process.returncode is not None


class Rpc:
    """Exchange real JSON-RPC requests without substituting any server component."""

    def __init__(self, websocket: ClientConnection) -> None:
        self.websocket = websocket
        self.identifier = 0

    async def request(self, method: str, **params: object) -> dict[str, Any]:
        self.identifier += 1
        await self.websocket.send(
            json.dumps(
                {"jsonrpc": "2.0", "id": self.identifier, "method": method, "params": params}
            )
        )
        response = json.loads(await asyncio.wait_for(self.websocket.recv(), 20))
        assert isinstance(response, dict) and response["id"] == self.identifier, response
        return response

    async def result(self, method: str, **params: object) -> dict[str, Any]:
        response = await self.request(method, **params)
        assert "error" not in response, response
        result = response["result"]
        assert isinstance(result, dict), response
        return result

    async def command(self, command: str) -> str:
        return "".join((await self.result("command.execute", command=command))["output"])

    async def stopped(self) -> None:
        async with asyncio.timeout(10):
            while (await self.result("target.status"))["state"] != "stopped":
                await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    ("machine", "explicit_endpoint"),
    [
        ("netduinoplus2", True),
        ("netduinoplus2", False),
        ("stm32vldiscovery", True),
        ("b-l475e-iot01a", True),
    ],
)
def test_qemu_websocket_execution(
    tmp_path: Path, tools: tuple[str, str], machine: str, explicit_endpoint: bool
) -> None:
    """Boot STM32 firmware, inspect RAM, hit a breakpoint and step through the real API."""

    async def exercise() -> None:
        config = _configuration(tmp_path, tools, machine, explicit_endpoint=explicit_endpoint)
        async with _running(config) as server:
            async with connect(f"ws://127.0.0.1:{server.api_port}") as websocket:
                rpc = Rpc(websocket)
                assert (await rpc.result("server.status"))["ready"] is True
                assert (await rpc.result("target.status"))["state"] == "stopped"
                assert "qemu" in await rpc.command(
                    "python from pyGdbToolkit.ocd import get_ocd; print(get_ocd().identifier)"
                )
                cores = (await rpc.result("target.cores"))["cores"]
                assert len(cores) == 1
                assert cores[0]["id"] == 0
                assert cores[0]["scope"] == "emulated-cpu-cluster"
                assert cores[0]["identity_source"] == "qemu-gdb-thread"
                assert (await rpc.result("target.select_core", core=0))["core"]["selected"]
                assert (
                    "not discovered"
                    in (await rpc.request("target.select_core", core=99))["error"]["message"]
                )
                context = await rpc.result("target.context")
                assert "qemu" in json.dumps(context)
                assert "0x" in await rpc.command("x/wx 0xe000ed00")
                await rpc.command("set {unsigned int}0x20000100 = 0x12345678")
                assert "0x12345678" in await rpc.command("x/wx 0x20000100")
                await rpc.command("break *0x0800000c")
                await rpc.command("continue")
                await rpc.stopped()
                assert "0x2b" in await rpc.command("p/x $r0")
                await rpc.command("stepi")
                await rpc.stopped()
                assert "0x800000c" in await rpc.command("p/x $pc")
                await rpc.command("monitor system_reset")
                assert "0x8000008" in await rpc.command("p/x $pc")

    asyncio.run(exercise())


def test_qemu_monitor_errors_are_explicit(tmp_path: Path, tools: tuple[str, str]) -> None:
    """Report unsupported HMP commands as RPC errors through both console and raw MI."""

    async def exercise() -> None:
        async with _running(_configuration(tmp_path, tools)) as server:
            async with connect(f"ws://127.0.0.1:{server.api_port}") as websocket:
                rpc = Rpc(websocket)
                assert "system_reset" in await rpc.command("monitor help")
                assert "R00=" in await rpc.command("monitor info registers")
                registers = await rpc.result("mi.execute", command="-data-evaluate-expression $pc")
                assert registers["class"] == "done"
                assert "0x8000008" in registers["record"]
                output = await rpc.result(
                    "mi.execute", command='-interpreter-exec console "p/x $pc"'
                )
                assert "0x8000008" in "".join(output["output"])
                for command in (
                    "monitor reset halt",
                    "monitor show aps",
                    "gdb monitor ReadAP 0",
                    "mon info nonexistent",
                ):
                    response = await rpc.request("command.execute", command=command)
                    assert response["error"]["code"] == -32000, response
                    assert (
                        "QEMU mode does not support monitor command" in response["error"]["message"]
                    )
                for command in (
                    '-interpreter-exec console "monitor reset halt"',
                    '-interpreter-exec "console" "monitor reset halt"  ',
                ):
                    response = await rpc.request("mi.execute", command=command)
                    assert response["error"]["code"] == -32000, response
                    assert "QEMU mode" in response["error"]["message"]
                response = await rpc.request(
                    "command.execute", command="monitor system_reset unexpected"
                )
                assert "QEMU monitor command" in response["error"]["message"]
                assert "failed" in response["error"]["message"]
                response = await rpc.request("command.execute", command="dap list")
                assert (
                    "QEMU mode does not expose a physical debug port"
                    in response["error"]["message"]
                )
                logs = await rpc.request("logs.get")
                assert "error" not in logs, logs
                assert "QEMU mode" in json.dumps(logs)
                assert "0x" in await rpc.command("x/wx 0xe000ed00")

    asyncio.run(exercise())


def test_generic_ocd_keeps_native_monitor_responses(tmp_path: Path, tools: tuple[str, str]) -> None:
    """Only the QEMU adapter rewrites monitor requests; plain MI preserves HMP output."""
    executable = tmp_path / "emulator"
    executable.symlink_to(tools[0])
    config = replace(_configuration(tmp_path, tools), ocd_path=str(executable), ocd_identifier="")
    assert config.backend_identifier == ""

    async def exercise() -> None:
        async with _running(config) as server:
            async with connect(f"ws://127.0.0.1:{server.api_port}") as websocket:
                rpc = Rpc(websocket)
                for method, command in (
                    ("command.execute", "monitor reset halt"),
                    ("mi.execute", '-interpreter-exec console "monitor reset halt"'),
                ):
                    result = await rpc.result(method, command=command)
                    assert result["class"] == "done"
                    assert "unknown command: 'reset'" in "".join(result["output"])

    asyncio.run(exercise())


def test_qemu_rejects_unsupported_startup_monitor(tmp_path: Path, tools: tuple[str, str]) -> None:
    """An unsupported initialization command fails startup and tears down both children."""

    async def exercise() -> None:
        config = replace(_configuration(tmp_path, tools), gdb_init=("monitor reset halt",))
        server = PyGdbServer(config)
        try:
            with pytest.raises(RuntimeError, match="QEMU mode does not support"):
                await server.start()
            assert server.api_port == 0
        finally:
            await server.stop()
        assert server.ocd is not None and server.ocd.process is not None
        assert server.ocd.process.returncode is not None
        assert server.mi.process is not None and server.mi.process.returncode is not None

    asyncio.run(exercise())


def test_qemu_failed_process_is_reported(tmp_path: Path, tools: tuple[str, str]) -> None:
    """A missing firmware is an explicit startup failure, not a ready API."""

    async def exercise() -> None:
        config = _configuration(tmp_path, tools)
        arguments = list(config.ocd_args)
        arguments[arguments.index("-kernel") + 1] = str(tmp_path / "missing.bin")
        server = PyGdbServer(replace(config, ocd_args=tuple(arguments)))
        try:
            with pytest.raises(RuntimeError, match="OCD exited"):
                await server.start()
            assert server.api_port == 0
            assert server.mi.process is None
            assert "missing.bin" in json.dumps(server.logs.get())
        finally:
            await server.stop()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "arguments",
    [
        ("-s",),
        ("-daemonize",),
        ("-gdb", "tcp:127.0.0.1:3333"),
        ("-gdb", "tcp:0.0.0.0:{gdb_port}"),
        ("-gdb",),
        ("-gdb", "tcp:{loopback}:{gdb_port}", "-gdb", "tcp:{loopback}:{gdb_port}"),
    ],
)
def test_qemu_invalid_endpoint_fails_before_launch(
    tmp_path: Path, tools: tuple[str, str], arguments: tuple[str, ...]
) -> None:
    """Refuse unsupervised or unsafe launch modes without spawning QEMU."""

    async def exercise() -> None:
        config = _configuration(tmp_path, tools, explicit_endpoint=False)
        server = PyGdbServer(replace(config, ocd_args=(*config.ocd_args, *arguments)))
        try:
            with pytest.raises(ValueError, match="QEMU"):
                await server.start()
            assert server.ocd is None
            assert server.mi.process is None
        finally:
            await server.stop()

    asyncio.run(exercise())
