# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Tests for the architecture-neutral pyOCD monitor transport."""

import pytest

from pyGdbToolkit.debug_port import DebugPortError, PyOcdMonitorTransport


def test_list_and_select() -> None:
    calls = []
    replies = iter(
        [
            "2 APs:\n0: AHB-AP (selected)\n2: APB-AP\n",
            "",
            "2 APs:\n0: AHB-AP\n2: APB-AP (selected)\n",
        ]
    )

    def execute(command: str) -> str:
        calls.append(command)
        return next(replies)

    transport = PyOcdMonitorTransport(execute)
    ports = transport.list_access_ports()
    assert [port.index for port in ports] == [0, 2]
    assert ports[0].selected
    assert not ports[1].selected
    transport.select_access_port(2)
    assert calls == ["monitor show aps", "monitor set mem-ap 0x2", "monitor show aps"]


def test_read_register() -> None:
    transport = PyOcdMonitorTransport(lambda command: "AP register 0x20000fc = 0x24770011\n")
    assert transport.read_ap(2, 0xFC) == 0x24770011


@pytest.mark.parametrize(
    "output", ["Unknown command", "Target is locked", "2 APs:\n0: AHB-AP", "1 APs:\nDP1:0: APB-AP"]
)
def test_list_rejects_errors_and_unsupported_addresses(output: str) -> None:
    with pytest.raises(DebugPortError):
        PyOcdMonitorTransport(lambda command: output).list_access_ports()


@pytest.mark.parametrize(
    "output", ["Error: transfer fault", "AP register 0xfc = 0x1234", "nothing"]
)
def test_read_rejects_errors_and_wrong_addresses(output: str) -> None:
    with pytest.raises(DebugPortError):
        PyOcdMonitorTransport(lambda command: output).read_ap(2, 0xFC)


def test_selection_must_be_confirmed() -> None:
    with pytest.raises(DebugPortError, match="did not select"):
        PyOcdMonitorTransport(
            lambda command: "2 APs:\n0: AHB-AP (selected)\n1: AHB-AP"
        ).select_access_port(1)


def test_apv2_uses_absolute_register_addresses() -> None:
    calls = []

    def execute(command: str) -> str:
        calls.append(command)
        if command == "monitor show aps":
            return "2 APs:\n@0x2000: AHB5-AP (selected)\n@0x4000: AHB5-AP"
        return "AP register 0x2dfc = 0x34770008"

    transport = PyOcdMonitorTransport(execute)
    ports = transport.list_access_ports()
    assert ports[0].ap_version == 2
    assert ports[0].index == 0x2000
    assert transport.read_ap(0x2000, 0xDFC) == 0x34770008
    assert calls[-1] == "monitor readap 0x2dfc"
