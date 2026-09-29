# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Arm Cortex-M fault-analysis diagnostic service."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from pyGdbToolkit.arch import DiagnosticRuntimeAccess, DiagnosticServiceName
from pyGdbToolkit.arch.arm import CortexMFaultCollector, decode_cpuid, read_stacked_frame
from pyGdbToolkit.arch.arm.cortex_m import CortexMPart
from pyGdbToolkit.target_memory import TargetReadError


@dataclass
class Memory:
    """Deterministic target memory with explicit unavailable addresses."""

    values: dict[int, int]

    def read_bytes(self, address: int, size: int) -> bytes:
        """Read a test byte range."""
        return self.read_uint32(address).to_bytes(size, "little")

    def read_uint16(self, address: int) -> int:
        """Read a test halfword."""
        return self.read_uint32(address) & 0xFFFF

    def read_uint32(self, address: int) -> int:
        """Read one configured word or expose the production access error."""
        try:
            return self.values[address]
        except KeyError as error:
            raise TargetReadError(address, 4, "not mapped") from error


@dataclass
class Registers:
    """Architecture-neutral runtime register test adapter."""

    values: dict[str, int]
    requests: list[tuple[str, ...]] = field(default_factory=list)

    def read_first(self, names: tuple[str, ...]) -> int | None:
        """Return the first register value exposed under a requested name."""
        self.requests.append(names)
        return next((self.values[name] for name in names if name in self.values), None)


@dataclass
class Symbols:
    """Architecture-neutral symbol resolver recording delegated addresses."""

    calls: list[int] = field(default_factory=list)

    def resolve(self, address: int) -> str:
        """Return a deterministic name and retain every resolution request."""
        self.calls.append(address)
        return f"symbol_{address:08X}"


def _target() -> object:
    """Return a known Cortex-M4 target description."""
    return decode_cpuid(0x410FC240)


def _m23_target() -> object:
    """Return a known Cortex-M23 target description."""
    return decode_cpuid(0x410F0000 | (CortexMPart.M23.value << 4))


def _access(registers: dict[str, int], symbols: Symbols) -> DiagnosticRuntimeAccess:
    """Create the neutral runtime access bundle used by the collector."""
    return DiagnosticRuntimeAccess(Registers(registers), symbols)


def _scb_memory() -> Memory:
    """Return fault registers that demonstrate typed SCB collection and diagnostics."""
    return Memory(
        {
            0xE000ED28: (1 << 9) | (1 << 15),
            0xE000ED2C: 1 << 30,
            0xE000ED30: 0,
            0xE000ED34: 0,
            0xE000ED38: 0,
            0xE000ED08: 0x08000000,
            0xE000ED24: (1 << 16) | (1 << 17) | (1 << 18),
            0xE000ED88: 0x00F00000,
        }
    )


def test_collector_returns_ordered_generic_report_and_delegates_symbols() -> None:
    """The service owns all Arm details but returns generic tables and panels."""
    memory = _scb_memory()
    memory.values.update(
        {
            0x20001000: 1,
            0x20001004: 2,
            0x20001008: 3,
            0x2000100C: 4,
            0x20001010: 12,
            0x20001014: 0x08000201,
            0x20001018: 0x08000101,
            0x2000101C: (1 << 24) | 3,
        }
    )
    symbols = Symbols()

    report = CortexMFaultCollector().collect(
        memory,
        _target(),  # type: ignore[arg-type]
        _access(
            {
                "pc": 0x08000021,
                "lr": 0xFFFFFFFD,
                "xpsr": (1 << 24) | 3,
                "ipsr": 3,
                "psp": 0x20001000,
            },
            symbols,
        ),
    )

    assert report.service is DiagnosticServiceName.FAULT_ANALYSIS
    assert [table.title for table in report.tables] == [
        "ARM Cortex-M Fault Overview",
        (
            "Stacked frame at crash time (Stack: PSP @ 0x20001000 | Return to: Thread "
            "(Non-Secure) | Frame: Basic (8 registers))"
        ),
        "SCB (System Control Block) Status Registers",
    ]
    assert report.tables[0].rows[1].values == ("Active Exception", "HardFault (Exception #3)")
    assert report.tables[1].rows[6].values == (
        "pc",
        "0x08000101",
        "<-- Faulting instruction: symbol_08000101",
    )
    assert report.panels[0].title == "Diagnostics & Probable Causes"
    assert report.panels[0].lines[:2] == (
        "• HardFault escalation: A BusFault was forced to HardFault (source handler disabled in SHCSR).",
        "• NULL pointer dereference: Invalid memory access at 0x00000000 (NULL-pointer / Vector Table).",
    )
    assert symbols.calls == [0x08000021, 0x08000201, 0x08000101, 0x08000101, 0x08000201]


def test_extended_frame_selects_psp_and_offsets_core_registers_after_fpu_state() -> None:
    """EXC_RETURN bit 4 selects an extended frame with core state after FP words."""
    memory = Memory({0x20002000 + 4 * index: index for index in range(26)})

    frame = read_stacked_frame(memory, 0xFFFFFFED, 0x20002000, "PSP")

    assert frame is not None
    assert not isinstance(frame, str)
    assert frame.has_fpu
    assert frame.sp_name == "PSP"
    assert frame.return_mode == "Thread"
    assert frame.target_domain == "Non-Secure"
    assert frame.fp_registers == tuple(range(16))
    assert frame.fpscr == 16
    assert (frame.r0, frame.lr, frame.pc, frame.xpsr) == (18, 23, 24, 25)


@pytest.mark.parametrize(
    ("exc_return", "selected_name", "other_name", "selected_stack", "other_stack"),
    (
        (0xFFFFFFFD, "psp_s", "psp_ns", 0x20004000, 0x20005000),
        (0xFFFFFFBD, "psp_ns", "psp_s", 0x20006000, 0x20007000),
        (0xFFFFFFF9, "msp_s", "msp_ns", 0x20008000, 0x20009000),
        (0xFFFFFFB9, "msp_ns", "msp_s", 0x2000A000, 0x2000B000),
    ),
)
def test_collector_prefers_exc_return_selected_banked_stack_register(
    exc_return: int,
    selected_name: str,
    other_name: str,
    selected_stack: int,
    other_stack: int,
) -> None:
    """EXC_RETURN[6] selects the secure bank before the unbanked stack alias."""
    memory = Memory(
        {
            selected_stack + 4 * index: index
            for index in range(8)
        }
        | {
            other_stack + 4 * index: 0xDEADBEEF
            for index in range(8)
        }
    )
    registers = Registers(
        {
            "lr": exc_return,
            selected_name: selected_stack,
            other_name: other_stack,
            "psp": other_stack,
        }
    )

    report = CortexMFaultCollector().collect(
        memory,
        _m23_target(),  # type: ignore[arg-type]
        DiagnosticRuntimeAccess(registers, Symbols()),
    )

    stack_alias = "psp" if exc_return & (1 << 2) else "msp"
    assert (selected_name, stack_alias) in registers.requests
    assert f"Stack: {stack_alias.upper()} @ 0x{selected_stack:08X}" in report.tables[1].title
    assert report.tables[1].rows[0].values == ("r0", "0x00000000", "")


def test_collector_renders_cortex_m23_secure_fault_status_and_address() -> None:
    """Cortex-M23 Security Extension status is retained through typed SCB reads."""
    report = CortexMFaultCollector().collect(
        Memory({0xE000EDE4: (1 << 3) | (1 << 6), 0xE000EDE8: 0x20000040}),
        _m23_target(),  # type: ignore[arg-type]
        _access({}, Symbols()),
    )

    scb = next(table for table in report.tables if table.title.startswith("SCB "))
    assert next(row.values for row in scb.rows if row.values[0] == "SFSR") == (
        "SFSR",
        "0x00000048",
        "AUVIOL: Attribution unit violation (SAU/IDAU)\n"
        "SFARVALID: SFAR contains valid fault address",
    )
    assert next(row.values for row in scb.rows if row.values[0] == "SFAR") == (
        "SFAR",
        "0x20000040",
        "[VALID] Region: SRAM",
    )


def test_collector_retains_stack_read_error_and_unavailable_scb_registers() -> None:
    """Unavailable stack and SCB reads remain deterministic report data."""
    report = CortexMFaultCollector().collect(
        Memory({}),
        _target(),  # type: ignore[arg-type]
        _access({"lr": 0xFFFFFFF9, "msp": 0x20003000}, Symbols()),
    )

    assert report.tables[0].rows[3].values == ("Current PC", "Unknown")
    assert report.tables[1].rows[0].values == (
        "CFSR",
        "Unavailable",
        "Not present on this target or memory inaccessible",
    )
    assert report.blocks[1].title == "Stack Frame"
    assert report.blocks[1].lines == (
        "Cannot read stacked frame at 0x20003000: could not read 4 byte(s) at 0x20003000: not mapped",
    )
    causes = next(panel for panel in report.panels if panel.title == "Diagnostics & Probable Causes")
    assert causes.lines == ("• No obvious error conditions detected in SCB registers.",)
