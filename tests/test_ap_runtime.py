# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Transparent OCD transport dispatch tests."""

import pytest

from pyGdbToolkit.ap_runtime import AutoDebugPortTransport
from pyGdbToolkit.debug_port import DebugPortError
from pyGdbToolkit.ocd import OcdDetector


def test_transparent_backend_switch_when_connection_changes() -> None:
    key = [(1, 1)]

    def execute(command: str) -> str:
        if key[0] == (1, 1):
            if command == "monitor echo [version]":
                raise RuntimeError("unknown command")
            if command == "monitor show aps":
                return "1 APs:\n0: AHB-AP (selected)\n"
            return "AP register 0xfc = 0x24770011"
        if command == "monitor echo [version]":
            return "Open On-Chip Debugger 0.12.0"
        if "cget -dap" in command:
            return "board.dap"
        if "dpreg 0" in command:
            return "0x2ba01477"
        if "apsel" in command:
            return "0x0"
        if command.startswith("monitor for "):
            return "PYGDB_AP 0 0x24770011\nPYGDB_AP_END"
        return "0x24770011"

    transport = AutoDebugPortTransport(execute, OcdDetector(execute, lambda: key[0]))
    assert transport.list_access_ports()[0].selected
    assert transport.backend_name == "PyOcdMonitorTransport"
    assert transport.read_ap(0, 0xFC) == 0x24770011
    key[0] = (1, 2)
    assert transport.list_access_ports()[0].selected
    assert transport.backend_name == "OpenOcdMonitorTransport"
    assert transport.read_ap(0, 0xFC) == 0x24770011
    assert transport.discovery == "OpenOCD ADIv5 IDR scan"


def test_unknown_ocd_never_defaults_to_pyocd() -> None:
    execute = lambda command: "unsupported"
    transport = AutoDebugPortTransport(execute, OcdDetector(execute, lambda: (1, 1)))
    with pytest.raises(DebugPortError, match="No supported OCD"):
        transport.list_access_ports()


def test_jlink_never_defaults_to_pyocd() -> None:
    """Use explicitly addressed J-Link reads, not pyOCD-specific commands."""
    calls = []

    def execute(command: str) -> str:
        calls.append(command)
        if command == "monitor help":
            return "SEGGER J-Link GDB Server V9.82"
        if command == "monitor ReadDP 0":
            return "O.K.:0x5BA00477"
        if command == "monitor ReadDP 2":
            return "O.K.:0x04000000"
        if command.startswith("monitor WriteDP 2 "):
            return "O.K."
        if command == "monitor ReadAPEx 0x1000000 0xfc":
            return "O.K.:0x44770002"
        if command.startswith("monitor ReadAPEx "):
            return "O.K.:0x00000000"
        return "Target does not support this command."

    transport = AutoDebugPortTransport(execute, OcdDetector(execute, lambda: (1, 1)))
    assert [port.index for port in transport.list_access_ports()] == [1]
    assert transport.backend_name == "JLinkMonitorTransport"
    assert transport.discovery.startswith("J-Link ADIv5 IDR scan")
    assert transport.read_ap(1, 0xFC) == 0x44770002
    assert calls[:4] == [
        "monitor echo [version]",
        "monitor show aps",
        "monitor help",
        "monitor ReadDP 0",
    ]
