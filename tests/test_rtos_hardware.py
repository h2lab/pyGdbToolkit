# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Opt-in RTOS acceptance on an already flashed Nucleo U5A5; never flash from pytest."""

import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import sysconfig

import pytest

from pyGdbClient.audit import AuditContextManager
from pyGdbClient.llm.sources import SourceBrowser, SourceError
from pyGdbClient.project import LoadedProject, load_project
from pyGdbClient.rpc import JsonRpcClient, RpcError
from pyGdbServer.config import load_config
from pyGdbServer.server import PyGdbServer

pytestmark = pytest.mark.skipif(
    os.environ.get("PYGDB_RTOS_HARDWARE") != "1",
    reason="requires explicitly configured RTOS firmware and probe; see doc/rtos-validation.md",
)
ROOT = Path(__file__).resolve().parents[1]
THREADS = (
    "validation_sem_thread",
    "validation_mutex_thread",
    "validation_event_any",
    "validation_event_all",
    "validation_sleeper",
    "validation_suspended",
)
REGISTERS = (
    "{'pc': int(gdb.newest_frame().pc()), "
    "'sp': int(gdb.newest_frame().read_register('sp')), "
    "'frame': gdb.selected_frame().level(), 'thread': gdb.selected_thread().global_num}"
)


async def _command(client: JsonRpcClient, command: str) -> str:
    result = await client.request("command.execute", {"command": command}, timeout=30)
    assert result["class"] == "done", result
    return re.sub(r"\x1b\[[0-9;]*m", "", "".join(result["output"]))


async def _json(client: JsonRpcClient, expression: str) -> dict[str, object]:
    output = await _command(
        client, f"python import json; print('PYGDB_JSON:' + json.dumps({expression}))"
    )
    line = next(line for line in output.splitlines() if line.startswith("PYGDB_JSON:"))
    value = json.loads(line.removeprefix("PYGDB_JSON:"))
    assert isinstance(value, dict)
    return value


async def _continue_until_stopped(client: JsonRpcClient) -> None:
    while not client.notifications.empty():
        client.notifications.get_nowait()
    resumed = await client.request("mi.execute", {"command": "-exec-continue"})
    assert resumed["class"] == "running", resumed
    running = False
    try:
        async with asyncio.timeout(20):
            while True:
                notification = await client.notifications.get()
                event = notification.get("params", {})
                if event.get("source") != "gdb" or event.get("stream") != "mi":
                    continue
                message = event["message"]
                if message.startswith("*running"):
                    running = True
                elif running and message.startswith("*stopped"):
                    return
    except TimeoutError:
        await client.request("mi.execute", {"command": "-exec-interrupt"})
        pytest.fail("RTOS scenario did not reach its expected stop within 20 seconds")


async def _zephyr(client: JsonRpcClient) -> None:
    await _command(client, "tbreak validation_checkpoint")
    await _continue_until_stopped(client)
    checkpoint = await _json(
        client,
        "{'function': gdb.newest_frame().name(), "
        "'cycles': int(gdb.parse_and_eval('validation_cycles'))}",
    )
    assert checkpoint["function"] == "validation_checkpoint" and checkpoint["cycles"] > 0
    listing = await _command(client, "rtos show threads")
    for name in (*THREADS, "validation_main", "idle"):
        assert name in listing, listing
    assert "incomplete" not in listing.lower()
    await _command(client, "frame 1")
    before = await _json(client, REGISTERS)
    current = await _command(client, "rtos show task validation_main")
    assert "Live newest GDB frame" in current and "GDB #0" in current, current
    assert "validation_checkpoint" in current and "0xAA pattern estimate" in current, current
    for name in THREADS:
        output = await _command(client, f"rtos show task {name}")
        assert "Saved PC" in output and "Saved LR" in output, output
        assert "0xAA pattern estimate" in output and "SP-based occupancy" in output, output
        assert (
            "suspended"
            if name.endswith("suspended")
            else "pending" if name in THREADS[:4] else "sleeping"
        ) in output.lower(), output
    addresses = await _json(
        client,
        "{name: int(gdb.parse_and_eval('&' + name)) for name in "
        "('validation_sem', 'validation_mutex', 'validation_event')}",
    )
    fields = await _json(
        client,
        "{'count': int(gdb.parse_and_eval('validation_sem.count')), "
        "'limit': int(gdb.parse_and_eval('validation_sem.limit')), "
        "'locks': int(gdb.parse_and_eval('validation_mutex.lock_count')), "
        "'owner': int(gdb.parse_and_eval('validation_mutex.owner')), "
        "'current': int(gdb.parse_and_eval('_kernel.cpus[0].current')), "
        "'events': int(gdb.parse_and_eval('validation_event.events'))}",
    )
    assert fields["count"] == 0 and fields["limit"] == 1
    assert fields["locks"] == 2 and fields["owner"] == fields["current"]
    assert fields["events"] == 1
    for kind, symbol, expected in (
        ("sem", "validation_sem", ("0 / 1", "1 waiter(s)")),
        ("mutex", "validation_mutex", ("Recursive lock count", "2", "1 waiter(s)")),
        ("event", "validation_event", ("0x00000001", "0x00000006", "2 waiter(s)", "any", "all")),
    ):
        address = addresses[symbol]
        assert isinstance(address, int)
        output = await _command(client, f"rtos show object {kind} 0x{address:X}")
        for text in expected:
            assert text in " ".join(output.split()), output
    assert await _json(client, REGISTERS) == before
    await _command(client, "frame 0")


async def _camelot(client: JsonRpcClient, loaded: LoadedProject) -> None:
    assert {"hello", "sample-c-app"} <= set(loaded.task_names), loaded.task_names
    await _command(client, "tbreak handle_messages")
    await _continue_until_stopped(client)
    stopped = await _json(client, "{'function': gdb.newest_frame().name()}")
    assert stopped["function"] == "handle_messages", stopped
    before = await _json(client, REGISTERS)
    for name in (*loaded.task_names, "idle"):
        output = await _command(client, f"rtos show task {name}")
        for section in ("Stack", "Pending events", "Backtrace"):
            assert section in output, output
        if name == "sample-c-app":
            assert "live PSP" in output and "GDB" in output, output
        elif name != "idle":
            assert "saved pc" in output and "saved" in output, output
    assert await _json(client, REGISTERS) == before
    with pytest.raises(RpcError, match="Thread enumeration is unavailable"):
        await _command(client, "rtos show threads")
    with pytest.raises(RpcError, match="Object inspection is unavailable"):
        await _command(client, "rtos show object sem 0x20000000")


async def _scheduler(client: JsonRpcClient, rtos: str, loaded: LoadedProject) -> None:
    baseline = await _json(client, "{'breakpoints': len(gdb.breakpoints() or ())}")
    await _command(client, "rtos showsched 4")
    await _command(
        client,
        "python from pyGdbToolkit.cmd_rtos import RtosSessionState; "
        "from pyGdbToolkit.session import SESSION; "
        "validation_trace = SESSION.state(RtosSessionState).scheduler_trace",
    )
    await _continue_until_stopped(client)
    recorded = await _json(
        client,
        "{'observations': validation_trace.elections, 'limit': validation_trace.limit, "
        "'breakpoints': len(gdb.breakpoints() or ())}",
    )
    observations = recorded["observations"]
    assert isinstance(observations, list) and len(observations) == recorded["limit"] == 4
    assert recorded["breakpoints"] == baseline["breakpoints"]
    assert (await client.request("rtos.status"))["scheduler_trace"] is None
    if rtos == "zephyr":
        assert all(" -> " in entry for entry in observations), observations
        assert any("validation_main" in entry for entry in observations), observations
        stop = await _json(
            client,
            "{'pc': int(gdb.newest_frame().pc()), "
            "'entry': int(gdb.parse_and_eval('&z_arm_pendsv')) & ~1}",
        )
        assert stop["pc"] == stop["entry"], stop
    else:
        assert set(observations) <= {*loaded.task_names, "idle"}, observations
    await _command(client, "rtos showsched 3")
    await _command(client, "rtos showsched 2")
    assert (await client.request("rtos.status"))["scheduler_trace"]["limit"] == 2
    await _command(client, f"rtos select {'camelot' if rtos == 'zephyr' else 'zephyr'}")
    assert (await client.request("rtos.status"))["scheduler_trace"] is None
    assert await _json(client, "{'breakpoints': len(gdb.breakpoints() or ())}") == baseline


def test_project_runtime_audit_and_sources_on_hardware(tmp_path: Path) -> None:
    """Validate the flashed Zephyr fixture or Camelot sample without model calls or flashing."""
    rtos = os.environ["PYGDB_RTOS_TYPE"]
    assert rtos in {"zephyr", "camelot"}, "PYGDB_RTOS_TYPE must be zephyr or camelot"
    project = Path(os.environ["PYGDB_RTOS_PROJECT"]).resolve(strict=True)
    workspace = (
        Path(os.environ["PYGDB_RTOS_WORKSPACE"]).resolve(strict=True) if rtos == "zephyr" else None
    )
    config = replace(
        load_config(os.environ["PYGDB_RTOS_SERVER_CONFIG"]),
        listen_host="127.0.0.1",
        listen_port=0,
        log_directory=tmp_path / "server-logs",
        gdb_args=(
            "-iex",
            "set mi-async on",
            "-iex",
            f"python import sys; sys.path[:0] = "
            f"{[str(ROOT / 'src'), sysconfig.get_paths()['purelib']]!r}",
        ),
        gdb_init=(
            "set arch arm",
            "monitor reset halt",
            "set mem inaccessible-by-default off",
        ),
    )

    async def exercise() -> None:
        server = PyGdbServer(config)
        client = None
        try:
            await server.start()
            client = JsonRpcClient(f"ws://127.0.0.1:{server.api_port}")
            await client.connect()
            await client.request("logs.subscribe")
            assert (await client.request("server.status"))["ready"] is True
            loaded = await load_project(client, project, rtos, workspace=workspace)
            await _command(
                client, "python from pyGdbToolkit.cmd_rtos import CONSOLE; CONSOLE.width = 180"
            )
            assert loaded.snapshot["selected_rtos"] == rtos and loaded.snapshot["loaded"] is True
            for artifact in loaded.artifacts:
                data = (project / artifact.path).read_bytes()
                assert artifact.size == len(data)
                assert artifact.sha256 == hashlib.sha256(data).hexdigest()
            show = await _command(client, "rtos show")
            assert ("Zephyr" if rtos == "zephyr" else "Kernel") in show, show
            audit = AuditContextManager()
            audit.set_project(loaded)
            document = (await audit.refresh(client)).to_json()
            assert document["project"]["rtos"] == rtos, document["project"]
            assert len(document["project"]["artifacts"]) == len(loaded.artifacts)
            if rtos == "zephyr":
                assert len(loaded.files) == 4 and loaded.source_workspace is not None
                browser = SourceBrowser.for_workspace(loaded.source_workspace)
                paths = (
                    "application/src/main.c",
                    "zephyr/kernel/sched.c",
                    "module-hal_stm32/stm32cube/stm32u5xx/drivers/src/stm32u5xx_hal.c",
                )
                await _zephyr(client)
            else:
                browser = SourceBrowser(project)
                paths = ("project.toml", "output/src/sample_c_app/src/main.c")
                await _camelot(client, loaded)
            for path in paths:
                result = browser.execute("read", {"path": path, "offset": 1, "limit": 5})
                assert result["path"] == path and result["content"]
            with pytest.raises(SourceError):
                browser.execute("read", {"path": "../outside"})
            await _scheduler(client, rtos, loaded)
        finally:
            if client is not None:
                await client.close()
            await server.stop()
        assert server.ocd is not None and server.ocd.process is not None
        assert server.ocd.process.returncode is not None
        assert server.mi.process is not None and server.mi.process.returncode is not None

    asyncio.run(exercise())
