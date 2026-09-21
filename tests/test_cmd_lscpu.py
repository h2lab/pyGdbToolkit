"""Tests for the public ``lscpu`` GDB command and Rich renderer."""

# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from pyGdbToolkit import cmd_lscpu
from pyGdbToolkit.coresight import MCU_ROM_TABLE_ADDRESS

_CIDR_OFFSETS = (0xFF0, 0xFF4, 0xFF8, 0xFFC)
_PIDR_OFFSETS = (0xFE0, 0xFE4, 0xFE8, 0xFEC, 0xFD0)
_FORBIDDEN_LEGACY_ADDRESSES = {
    0x40015800,
    0xE0042000,
    0xE0044000,
    0x44024000,
    0x5C001000,
    0x44001000,
    0x46001000,
}


class FakeInferior:
    """A byte-addressable target inferior."""

    def __init__(self, values: dict[int, bytes]) -> None:
        """Store exact readable target byte ranges."""
        self.values = values
        self.calls: list[tuple[int, int]] = []

    def read_memory(self, address: int, size: int) -> bytes:
        """Read an exact target range or report an inaccessible address."""
        self.calls.append((address, size))
        if address not in self.values:
            import gdb

            raise gdb.MemoryError("not mapped")
        return self.values[address][:size]


def _little_endian(value: int, size: int) -> bytes:
    """Encode a fake target register in Cortex-M byte order."""
    return value.to_bytes(size, byteorder="little")


def _rom_root_values(base: int, pidr: tuple[int, int, int, int, int]) -> dict[int, bytes]:
    """Build a valid, empty ROM root with the supplied PIDR bytes."""
    values: dict[int, bytes] = {base: _little_endian(0, 4)}
    cidr = (0x0D, 0x10, 0x05, 0xB1)
    for offset, value in zip(_CIDR_OFFSETS, cidr, strict=True):
        values[base + offset] = _little_endian(value, 4)
    for offset, value in zip(_PIDR_OFFSETS, pidr, strict=True):
        values[base + offset] = _little_endian(value, 4)
    return values


def test_command_rejects_arguments(fake_gdb: object) -> None:
    """The public command fails clearly when passed any argument."""
    del fake_gdb
    import gdb

    command = cmd_lscpu.LscpuCmd()

    with pytest.raises(gdb.GdbError, match="does not accept arguments"):
        command.invoke("unexpected", False)


def test_module_console_is_forced_terminal_and_command_prints_directly(
    fake_gdb: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The report uses the module console and produces terminal ANSI output."""
    del fake_gdb
    import gdb

    assert cmd_lscpu.CONSOLE._force_terminal is True
    stream = StringIO()
    capturing_console = Console(file=stream, force_terminal=True, width=100)
    monkeypatch.setattr(cmd_lscpu, "CONSOLE", capturing_console)

    values = _rom_root_values(MCU_ROM_TABLE_ADDRESS, (0x86, 0x04, 0x0A, 0x00, 0x00))
    values.update(
        {
            0xE000ED00: _little_endian(0x413FC241, 4),
            0x46009014: _little_endian(0x11111111, 4),
            0x46009018: _little_endian(0x22222222, 4),
            0x4600901C: _little_endian(0x33333333, 4),
        }
    )
    inferior = FakeInferior(values)
    gdb._inferior = inferior  # type: ignore[attr-defined]

    cmd_lscpu.LscpuCmd().invoke("", False)

    output = stream.getvalue()
    assert "\x1b[" in output
    assert "Cortex-M4" in output
    assert "MCU ROM JEP106 identity" in output
    assert "bank 0, code 0x20" in output
    assert "STM32N6 product line" in output
    assert "IDCODE" not in output
    assert all(address not in _FORBIDDEN_LEGACY_ADDRESSES for address, _ in inferior.calls)
