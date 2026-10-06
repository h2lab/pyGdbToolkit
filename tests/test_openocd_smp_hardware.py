# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Intrusive opt-in AArch64 SMP validation using OpenOCD and a J-Link adapter."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from pyGdbServer.config import load_config
from pyGdbServer.server import PyGdbServer

pytestmark = pytest.mark.skipif(
    os.environ.get("PYGDB_OPENOCD_SMP_HARDWARE") != "1"
    or os.environ.get("PYGDB_OPENOCD_ALLOW_INTRUSIVE") != "1",
    reason="requires the i.MX8MP/J-Link fixture and explicit permission to halt the SMP cluster",
)


def test_openocd_aarch64_cli_websocket_and_cpu_report(tmp_path: Path) -> None:
    """Verify all four contexts through the real supervised server and WebSocket API."""
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "doc/examples/boards/imx8mp-a53-openocd.json")
    args = tuple(
        str(root / argument) if argument.endswith("imx8mp-a53-openocd.cfg") else argument
        for argument in config.ocd_args
    )
    config = replace(config, ocd_args=args, listen_port=0, log_directory=tmp_path)

    async def exercise() -> None:
        server = PyGdbServer(config)
        try:
            await server.start()
            async with connect(f"ws://localhost:{server.api_port}") as websocket:
                identifier = 0

                async def request(method, **params):
                    nonlocal identifier
                    identifier += 1
                    await websocket.send(
                        json.dumps(
                            {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
                        )
                    )
                    response = json.loads(await asyncio.wait_for(websocket.recv(), 30))
                    assert response.get("id") == identifier
                    assert "error" not in response, response
                    return response["result"]

                inventory = await request("target.cores")
                assert [core["id"] for core in inventory["cores"]] == [0, 1, 2, 3]
                for core_id in (0, 1, 2, 3, 0, 3, 1, 0):
                    selected = await request("target.select_core", core=core_id)
                    assert selected["core"]["id"] == core_id
                    assert selected["core"]["inferior"] == 1
                    result = await request("command.execute", command=f"dap core {core_id}")
                    assert result["class"] == "done", result
                    target = await request(
                        "command.execute", command="monitor echo [target current]"
                    )
                    assert "".join(target["output"]).strip() == f"imx8mp.cpu{core_id}"
                    report = await request("command.execute", command="lscpu")
                    assert report["class"] == "done", report
                    output = "".join(report["output"])
                    assert "Cortex-A53" in output and "r0p4" in output
                    assert "OpenOCD external debug MIDR" in output
                    check = await request(
                        "command.execute",
                        command=(
                            "python address = int(gdb.selected_frame().read_register('pc')); "
                            "insert = gdb.execute(f'maintenance packet Z1,{address:x},4', to_string=True); "
                            "remove = gdb.execute(f'maintenance packet z1,{address:x},4', to_string=True); "
                            "assert 'received: \"OK\"' in insert, insert; "
                            "assert 'received: \"OK\"' in remove, remove"
                        ),
                    )
                    assert check["class"] == "done", check
                assert server.core_ocds == {}
        finally:
            await server.stop()
        assert server.ocd is not None and server.ocd.process is not None
        assert server.ocd.process.returncode is not None
        assert server.mi.process is None or server.mi.process.returncode is not None

    asyncio.run(exercise())
