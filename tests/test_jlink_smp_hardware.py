# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Intrusive opt-in A53 cluster validation through real pyGdbServer and GDB."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import os
from pathlib import Path

import pytest

from pyGdbServer.config import load_config
from pyGdbServer.server import PyGdbServer

pytestmark = pytest.mark.skipif(
    os.environ.get("PYGDB_JLINK_SMP_HARDWARE") != "1"
    or os.environ.get("PYGDB_JLINK_ALLOW_INTRUSIVE") != "1",
    reason="requires an i.MX8MP and explicit permission to halt its A53 cluster",
)


def test_a53_cluster_cli_rpc_affinity_halt_and_hardware_breakpoints(tmp_path: Path) -> None:
    """Verify real core pivots and breakpoint insertion without reset or instruction stepping."""
    config_path = Path(__file__).resolve().parents[1] / "doc/examples/boards/imx8mp-a53.json"
    config = replace(
        load_config(config_path), listen_port=0, log_directory=tmp_path, startup_timeout=30
    )

    async def exercise() -> None:
        server = PyGdbServer(config)
        try:
            await server.start()
            cores = (await server.target_cores("target.cores"))["cores"]
            assert [core["id"] for core in cores] == [0, 1, 2, 3]
            for core_id in (0, 1, 2, 3, 0, 3, 1, 0):
                selected = (await server.target_cores("target.select_core", core_id))["core"]
                assert selected["id"] == core_id
                cli = await server.mi.console(f"dap core {core_id}")
                assert cli.result_class == "done", cli
                result = await server.mi.console("monitor cp15 0,0,0,5")
                assert f"0x8000000{core_id}" in "".join(result.output), result
                halted = await server.mi.console("monitor IsHalted")
                assert "Halted" in "".join(halted.output), halted
                check = await server.mi.console(
                    "python address = int(gdb.selected_frame().read_register('pc')); "
                    "insert = gdb.execute(f'maintenance packet Z1,{address:x},4', to_string=True); "
                    "remove = gdb.execute(f'maintenance packet z1,{address:x},4', to_string=True); "
                    "assert 'received: \"OK\"' in insert, insert; "
                    "assert 'received: \"OK\"' in remove, remove"
                )
                assert check.result_class == "done", check
            assert len(server.core_ocds) == 3
        finally:
            await server.stop()
        assert server.ocd is not None and server.ocd.process is not None
        assert server.ocd.process.returncode is not None
        assert all(
            process.process is not None and process.process.returncode is not None
            for process in server.core_ocds.values()
        )

    asyncio.run(exercise())
