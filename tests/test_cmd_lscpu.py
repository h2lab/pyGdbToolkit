"""Tests for the public ``lscpu`` GDB command and Rich renderer."""

# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from pyGdbToolkit import cmd_lscpu
from pyGdbToolkit.arch import Architecture
from pyGdbToolkit.arch.aarch64.cpu import CPU_REGISTERS, CpuRegister, CpuReport, decode_midr
from pyGdbToolkit.arch.aarch64.target import AArch64TargetDescription
from pyGdbToolkit.arch.arm.coresight import MCU_ROM_TABLE_ADDRESS

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


@pytest.mark.parametrize("code", [0x15, 0x0E])
def test_command_renders_nxp_vendor_and_cpuid_core_without_signature_reads(
    fake_gdb: object, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """Render NXP Cortex-M7 identity from CPUID and ROM rather than OCD logs."""
    stream = StringIO()
    monkeypatch.setattr(cmd_lscpu, "CONSOLE", Console(file=stream, width=120))
    values = _rom_root_values(
        MCU_ROM_TABLE_ADDRESS,
        (0xC8, ((code & 0xF) << 4) | 4, ((code >> 4) & 7) | 8, 0, 0),
    )
    values[0xE000ED00] = _little_endian(0x411FC272, 4)
    inferior = FakeInferior(values)
    fake_gdb._inferior = inferior

    cmd_lscpu.LscpuCmd().invoke("", False)

    output = stream.getvalue()
    assert "NXP Semiconductors" in output
    assert "Cortex-M7" in output
    assert "r1p2" in output
    assert "no documented device profile" in output
    assert "IMX8MP" not in output
    assert inferior.calls.count((0xE000ED00, 4)) == 2
    assert all(
        address == 0xE000ED00 or MCU_ROM_TABLE_ADDRESS <= address < 0xE0100000
        for address, _ in inferior.calls
    )


def test_command_rejects_arguments(fake_gdb: object) -> None:
    """The public command fails clearly when passed any argument."""
    del fake_gdb
    import gdb

    command = cmd_lscpu.LscpuCmd()

    with pytest.raises(gdb.GdbError, match="does not accept arguments"):
        command.invoke("unexpected", False)


@pytest.mark.parametrize(
    "architecture,collector",
    ((Architecture.ARM, "cpu_arm_report"), (Architecture.AARCH64, "cpu_aarch64_report")),
)
def test_cpu_report_dispatches_only_the_matching_collector(
    monkeypatch: pytest.MonkeyPatch, architecture: Architecture, collector: str
) -> None:
    """The common CPU API preserves the report returned by exactly one architecture collector."""
    report = object()
    calls = []
    monkeypatch.setattr(cmd_lscpu, "SESSION", SimpleNamespace(architecture=architecture))

    def collect():
        calls.append(collector)
        return report

    for name in ("cpu_arm_report", "cpu_aarch64_report"):
        monkeypatch.setattr(
            cmd_lscpu,
            name,
            collect if name == collector else lambda: pytest.fail("wrong architecture collector"),
        )
    assert cmd_lscpu.cpu_report() is report
    assert calls == [collector]


@pytest.mark.parametrize("architecture", (Architecture.RISCV, Architecture.XTENSA, None))
def test_unsupported_architecture_never_falls_back_to_arm(
    monkeypatch: pytest.MonkeyPatch, architecture: Architecture | None
) -> None:
    """Unknown and unsupported architectures do not invoke either CPU collector."""
    import gdb

    command = cmd_lscpu.LscpuCmd()
    monkeypatch.setattr(
        cmd_lscpu,
        "SESSION",
        SimpleNamespace(
            architecture=architecture,
            probe=lambda: SimpleNamespace(unavailable_reason="no recognized architecture"),
        ),
    )
    monkeypatch.setattr(
        cmd_lscpu, "cpu_arm_report", lambda: pytest.fail("unexpected ARM collector")
    )
    monkeypatch.setattr(
        cmd_lscpu, "cpu_aarch64_report", lambda: pytest.fail("unexpected AArch64 collector")
    )

    message = (
        "architecture unavailable" if architecture is None else "does not support architecture"
    )
    with pytest.raises(gdb.GdbError, match=message):
        command.invoke("", False)


@pytest.mark.parametrize("expose_registers", (True, False))
def test_aarch64_command_never_reads_cortex_m_memory(
    fake_gdb: object, monkeypatch: pytest.MonkeyPatch, expose_registers: bool
) -> None:
    """AArch64 reports named system registers or unavailability without SoC memory reads."""
    import gdb

    values = {"midr_el1": 0x410FD034, "mpidr_el1": 0x80000003} if expose_registers else {}

    def read_register(name: str) -> int:
        if name not in values:
            raise gdb.error("register not exposed")
        return values[name]

    stream = StringIO()
    monkeypatch.setattr(cmd_lscpu, "CONSOLE", Console(file=stream, width=120))
    fake_gdb._inferior = SimpleNamespace(
        architecture=lambda: SimpleNamespace(name=lambda: "aarch64")
    )
    fake_gdb._frame = SimpleNamespace(read_register=read_register)

    cmd_lscpu.LscpuCmd().invoke("", False)

    output = stream.getvalue()
    assert "AArch64 CPU report" in output
    assert "MIDR_EL1" in output
    assert "not exposed or readable" in output
    assert "NXP" not in output
    assert "Cortex-M" not in output
    if expose_registers:
        assert "Cortex-A53" in output
        assert "r0p4" in output
        assert "0:0:0:3" in output
    else:
        assert "Cortex-A53" not in output


def test_aarch64_renderer_decodes_architectural_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """A complete register server can report features without a SoC-specific provider."""
    values = {
        "MIDR_EL1": 0x413FD082,
        "MPIDR_EL1": 0x1280345678,
        "ID_AA64PFR0_EL1": (1 << 16) | (1 << 20),
        "ID_AA64MMFR0_EL1": 2,
        "CTR_EL0": 0x84448004,
        "REVIDR_EL1": 0x123456789ABCDEF0,
        "CurrentEL": 12,
    }
    target = AArch64TargetDescription(Architecture.AARCH64, "Arm", "AArch64", "unknown", "aarch64")
    registers = tuple(CpuRegister(name, width, values.get(name)) for name, width in CPU_REGISTERS)
    report = CpuReport(target, registers, decode_midr(values["MIDR_EL1"]))
    stream = StringIO()
    monkeypatch.setattr(cmd_lscpu, "CONSOLE", Console(file=stream, width=140))

    cmd_lscpu.render_aarch64_report(report)

    output = stream.getvalue()
    assert "Cortex-A72" in output
    assert "r3p2" in output
    assert "18:52:86:120" in output
    assert "EL3" in output
    assert "Supported, including FP16" in output
    assert "40 bits" in output
    assert output.count("64 bytes") == 2
    assert "0x123456789ABCDEF0" in output
    assert "RAM" not in output
    assert "Vendor" not in output


def test_aarch64_renderer_keeps_partial_register_bits_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial J-Link register reads do not silently become complete 64-bit values."""
    target = AArch64TargetDescription(Architecture.AARCH64, "Arm", "AArch64", "unknown", "aarch64")
    registers = tuple(
        (
            CpuRegister(name, width, 0x84448004, 32, "J-Link CP15")
            if name == "CTR_EL0"
            else CpuRegister(name, width, None)
        )
        for name, width in CPU_REGISTERS
    )
    stream = StringIO()
    monkeypatch.setattr(cmd_lscpu, "CONSOLE", Console(file=stream, width=140))

    cmd_lscpu.render_aarch64_report(CpuReport(target, registers, None))

    assert "0x????????84448004 (J-Link CP15)" in stream.getvalue()
    assert "0x0000000084448004" not in stream.getvalue()


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
