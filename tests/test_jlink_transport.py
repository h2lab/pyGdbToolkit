# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Tests for J-Link's ADIv5 AP register transport."""

import pytest

from pyGdbToolkit.debug_port import DebugPortError, JLinkMonitorTransport


def _executor(read_ap, calls, selection=0x040000F0):
    state = [selection]

    def execute(command):
        calls.append(command)
        if command == "monitor ReadDP 0":
            return "O.K.:0x5BA00477"
        if command == "monitor ReadDP 2":
            return f"O.K.:0x{state[0]:08X}"
        if command.startswith("monitor WriteDP 2 "):
            state[0] = int(command.split()[-1], 0)
            return "O.K."
        if command.startswith("monitor ReadAPEx "):
            base, address = command.split()[-2:]
            state[0] = int(base, 0) | (int(address, 0) & 0xF0)
            return read_ap(command)
        raise AssertionError(command)

    return execute


def test_jlink_scan_and_local_selection() -> None:
    """Preserve gaps in APSEL and restore selection without accessing target memory."""
    calls = []

    def execute(command: str) -> str:
        if command == "monitor ReadAPEx 0x2000000 0xfc":
            return "O.K.:0x24770011"
        return "O.K.:0x00000000"

    transport = JLinkMonitorTransport(_executor(execute, calls))
    ports = transport.list_access_ports()
    assert [port.index for port in ports] == [2]
    assert not ports[0].selected
    assert len(calls) == 260
    assert calls[-2:] == ["monitor WriteDP 2 0x40000f0", "monitor ReadDP 2"]
    transport.select_access_port(2)
    assert len(calls) == 260
    assert transport.list_access_ports()[0].selected
    assert all("WriteAP" not in command and "ReadMem" not in command for command in calls)
    with pytest.raises(DebugPortError, match="not discovered"):
        transport.select_access_port(1)


def test_jlink_register_addressing() -> None:
    """Use a shifted APSEL and a byte offset, not a register index."""
    calls = []

    def execute(command: str) -> str:
        return "O.K.:0x64770001\n"

    assert JLinkMonitorTransport(_executor(execute, calls)).read_ap(4, 0xFC) == 0x64770001
    assert calls == [
        "monitor ReadDP 0",
        "monitor ReadDP 2",
        "monitor ReadAPEx 0x4000000 0xfc",
        "monitor WriteDP 2 0x40000f0",
        "monitor ReadDP 2",
    ]


@pytest.mark.parametrize("index,address", [(-1, 0), (256, 0), (0, -4), (0, 0x100), (0, 3)])
def test_jlink_rejects_invalid_addresses(index: int, address: int) -> None:
    """Validate inputs before sending any monitor request."""

    def execute(command: str) -> str:
        raise AssertionError(command)

    with pytest.raises(DebugPortError, match="Invalid J-Link APv1"):
        JLinkMonitorTransport(execute).read_ap(index, address)


@pytest.mark.parametrize(
    "output", ["ERROR:Read failed", "O.K.", "O.K.:0x100000000", "O.K.:0x1\nERROR:Fault"]
)
def test_jlink_rejects_bad_responses(output: str) -> None:
    """Do not report malformed reads or transport failures as AP absence."""
    calls = []
    with pytest.raises(DebugPortError, match="Invalid J-Link register response"):
        JLinkMonitorTransport(_executor(lambda command: output, calls)).read_ap(0, 0xFC)
    assert calls[-2:] == ["monitor WriteDP 2 0x40000f0", "monitor ReadDP 2"]


def test_jlink_wraps_monitor_errors() -> None:
    """Expose failed monitor execution through the shared transport contract."""

    def execute(command: str) -> str:
        raise RuntimeError("connection lost")

    calls = []
    with pytest.raises(DebugPortError, match="J-Link monitor: connection lost"):
        JLinkMonitorTransport(_executor(execute, calls)).read_ap(0, 0xFC)
    assert calls[-2:] == ["monitor WriteDP 2 0x40000f0", "monitor ReadDP 2"]


@pytest.mark.parametrize("dpidr", [0, 0xFFFFFFFF, 0x2BA01477, 0x6BA03477])
def test_jlink_does_not_scan_unsupported_debug_ports(dpidr: int) -> None:
    """Do not apply an ADIv5 scan to missing debug ports or ADIv6."""
    calls = []

    def execute(command: str) -> str:
        calls.append(command)
        return f"O.K.:0x{dpidr:08X}"

    with pytest.raises(DebugPortError, match="requires JTAG-DPv0"):
        JLinkMonitorTransport(execute).list_access_ports()
    assert calls == ["monitor ReadDP 0"]


def test_jlink_scan_failure_is_not_a_partial_inventory() -> None:
    """An inaccessible AP is not silently treated as absent."""

    def execute(command: str) -> str:
        if command == "monitor ReadAPEx 0x0 0xfc":
            return "O.K.:0x64770001"
        return "ERROR:Read failed"

    calls = []
    with pytest.raises(DebugPortError, match="Read failed"):
        JLinkMonitorTransport(_executor(execute, calls)).list_access_ports()
    assert calls[-2:] == ["monitor WriteDP 2 0x40000f0", "monitor ReadDP 2"]


def test_jlink_initial_selection_comes_from_the_debug_port() -> None:
    """Do not invent the AP selected by the server."""
    transport = JLinkMonitorTransport(_executor(lambda command: "O.K.:0x64770001", []))
    assert [port.index for port in transport.list_access_ports() if port.selected] == [4]


def test_jlink_refuses_unverified_selection_restoration() -> None:
    """A restoration failure must not look like a successful AP read."""
    execute = _executor(lambda command: "O.K.:0x64770001", [])

    def failed_restore(command: str) -> str:
        if command.startswith("monitor WriteDP "):
            return "ERROR:Write failed"
        return execute(command)

    with pytest.raises(DebugPortError, match="did not restore"):
        JLinkMonitorTransport(failed_restore).read_ap(0, 0xFC)
