# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""OpenOCD monitor transport tests with observed RP2350 response formats."""

import pytest

from pyGdbToolkit.debug_port import DebugPortError, OpenOcdMonitorTransport

ROOT = """AP # 0x0
Dev Arch is 0x47700af7, ARM Ltd \"CoreSight ROM architecture\"
AP # 0x2000
Dev Arch is 0x47700a17, ARM Ltd \"Memory Access Port v2 architecture\"
AP ID register 0x34770008
Type is MEM-AP AHB5 with enhanced HPROT
AP # 0x4000
AP ID register 0x34770008
Type is MEM-AP AHB5 with enhanced HPROT
"""


class Monitor:
    def __init__(self) -> None:
        self.calls = []
        self.selected = 0x2000
        self.dpidr = 0x4C013477
        self.root = ROOT

    def __call__(self, command: str) -> str:
        self.calls.append(command)
        if "cget -dap" in command:
            return "rp2350.dap\navailable\n"
        if "dpreg 0" in command:
            return f"0x{self.dpidr:08x}\navailable\n"
        if command == "monitor echo [rp2350.dap apsel]":
            return f"0x{self.selected:x}\n"
        if "apsel " in command:
            self.selected = int(command.split()[-1].rstrip("]"), 0)
            return "available\n"
        if "info root" in command:
            return self.root
        if "apreg" in command:
            return "0x34770008\navailable\n"
        if command.startswith("monitor for "):
            return "PYGDB_AP 0 0x24770011\nPYGDB_AP 3 0x04760000\nPYGDB_AP_END\n"
        raise AssertionError(command)


def test_apv2_discovery_excludes_root_table() -> None:
    monitor = Monitor()
    ports = OpenOcdMonitorTransport(monitor).list_access_ports()
    assert [port.index for port in ports] == [0x2000, 0x4000]
    assert all(port.ap_version == 2 for port in ports)
    assert ports[0].selected


def test_explicit_apv2_read_and_selection() -> None:
    monitor = Monitor()
    transport = OpenOcdMonitorTransport(monitor)
    transport.list_access_ports()
    assert transport.read_ap(0x2000, 0xDFC) == 0x34770008
    assert monitor.calls[-1] == "monitor echo [rp2350.dap apreg 0x2000 0xdfc]"
    transport.select_access_port(0x4000)
    assert monitor.selected == 0x4000


def test_adiv5_discovery_uses_bounded_idr_scan() -> None:
    monitor = Monitor()
    monitor.dpidr = 0x2BA01477
    monitor.selected = 3
    ports = OpenOcdMonitorTransport(monitor).list_access_ports()
    assert [port.index for port in ports] == [0, 3]
    assert ports[1].selected
    assert all(port.ap_version == 1 for port in ports)
    assert "info root" not in " ".join(monitor.calls)


@pytest.mark.parametrize("index,address", [(0x2001, 0xDFC), (-1, 0), (0x2000, 0x1000), (0x2000, 3)])
def test_invalid_register_requests_are_rejected(index, address) -> None:
    transport = OpenOcdMonitorTransport(Monitor())
    with pytest.raises(DebugPortError):
        transport.read_ap(index, address)


def test_root_errors_are_not_an_empty_inventory() -> None:
    monitor = Monitor()
    monitor.root = "unsupported"
    with pytest.raises(DebugPortError, match="root ROM table"):
        OpenOcdMonitorTransport(monitor).list_access_ports()


def test_dap_name_is_not_interpolated_as_arbitrary_tcl() -> None:
    with pytest.raises(DebugPortError, match="usable DAP"):
        OpenOcdMonitorTransport(lambda command: "evil; shutdown").list_access_ports()


@pytest.mark.parametrize("output", ["available", "PYGDB_AP_ERROR 2\nPYGDB_AP_END"])
def test_incomplete_adiv5_scan_is_not_an_empty_inventory(output) -> None:
    monitor = Monitor()
    monitor.dpidr = 0x2BA01477

    def execute(command: str) -> str:
        return output if command.startswith("monitor for ") else monitor(command)

    with pytest.raises(DebugPortError):
        OpenOcdMonitorTransport(execute).list_access_ports()
