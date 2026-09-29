# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Arm Cortex-M fault-analysis diagnostic service."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Callable

from ...target_memory import TargetMemory, TargetReadError
from ..base import Architecture, SystemRegisterSet, TargetDescription
from ..diagnostics import (
    DiagnosticPanel,
    DiagnosticReport,
    DiagnosticRuntimeAccess,
    DiagnosticServiceName,
    DiagnosticTable,
    DiagnosticTableRow,
)
from .cortex_m import CortexMTargetDescription, read_scb

_EXC_RETURN_MASK = 0xFFFFFF00
_EXCEPTION_NAMES: dict[int, str] = {
    1: "Reset",
    2: "NMI",
    3: "HardFault",
    4: "MemManage",
    5: "BusFault",
    6: "UsageFault",
    7: "SecureFault",
    11: "SVCall",
    12: "DebugMonitor",
    14: "PendSV",
    15: "SysTick",
}
_UFSR_BITS: dict[int, tuple[str, str]] = {
    0: ("UNDEFINSTR", "Undefined instruction executed"),
    1: ("INVSTATE", "Invalid Thumb state (bit T=0 or branch to even address)"),
    2: ("INVPC", "Invalid EXC_RETURN value on exception return"),
    3: ("NOCP", "Access to disabled or non-existent coprocessor/FPU"),
    4: ("STKOF", "Stack overflow detected"),
    8: ("UNALIGNED", "Unaligned access trapped (CCR.UNALIGN_TRP=1)"),
    9: ("DIVBYZERO", "Integer division by zero (CCR.DIV_0_TRP=1)"),
}
_BFSR_BITS: dict[int, tuple[str, str]] = {
    0: ("IBUSERR", "Bus error on instruction fetch"),
    1: ("PRECISERR", "Precise bus error on data access (BFAR valid)"),
    2: ("IMPRECISERR", "Imprecise bus error (asynchronous write buffer)"),
    3: ("UNSTKERR", "Bus error on exception return unstacking"),
    4: ("STKERR", "Bus error on exception entry stacking"),
    5: ("LSPERR", "Bus error during FPU lazy preservation"),
    7: ("BFARVALID", "BFAR contains valid fault address"),
}
_MMFSR_BITS: dict[int, tuple[str, str]] = {
    0: ("IACCVIOL", "MPU/security violation on instruction fetch (XN)"),
    1: ("DACCVIOL", "MPU/security violation on data access"),
    3: ("MUNSTKERR", "MPU violation on exception return unstacking"),
    4: ("MSTKERR", "MPU violation on exception entry stacking"),
    5: ("MLSPERR", "MPU violation during FPU lazy preservation"),
    7: ("MMARVALID", "MMFAR contains valid fault address"),
}
_HFSR_BITS: dict[int, tuple[str, str]] = {
    1: ("VECTTBL", "Vector table read error"),
    30: ("FORCED", "Fault escalated to HardFault (source handler disabled/masked)"),
    31: ("DEBUGEVT", "Debug event / breakpoint"),
}
_DFSR_BITS: dict[int, tuple[str, str]] = {
    0: ("HALTED", "Core halted by debug request / DAP"),
    1: ("BKPT", "BKPT instruction executed"),
    2: ("DWTTRAP", "DWT trap / watchpoint"),
    3: ("VCATCH", "Vector catch triggered"),
    4: ("EXTERNAL", "External debug request"),
}
_SFSR_BITS: dict[int, tuple[str, str]] = {
    0: ("INVEP", "Invalid entry point (missing SG instruction)"),
    1: ("INVIS", "Invalid integrity signature"),
    2: ("INVER", "Invalid Secure exception return"),
    3: ("AUVIOL", "Attribution unit violation (SAU/IDAU)"),
    4: ("INVTRAN", "Illegal security domain transition"),
    5: ("LSPERR", "SAU/IDAU error during lazy preservation"),
    6: ("SFARVALID", "SFAR contains valid fault address"),
    7: ("LSERR", "Lazy state enable/disable error"),
}


@dataclass(frozen=True)
class StackedFrame:
    """Decoded hardware exception frame restored from the active stack."""

    lr_exc_return: int
    sp_name: str
    sp_address: int
    return_mode: str
    target_domain: str
    has_fpu: bool
    secure_stacking: bool
    r0: int
    r1: int
    r2: int
    r3: int
    r12: int
    lr: int
    pc: int
    xpsr: int
    fp_registers: tuple[int, ...] | None = None
    fpscr: int | None = None
    s0_float: float | None = None


def _is_exc_return(value: int) -> bool:
    """Return whether a value has the Cortex-M EXC_RETURN high-byte pattern."""
    return value & _EXC_RETURN_MASK == _EXC_RETURN_MASK


def _stack_register_names(exc_return: int, sp_name: str) -> tuple[str, str]:
    """Return the banked stack register selected by EXC_RETURN before its alias."""
    stack_register = sp_name.lower()
    security_suffix = "s" if exc_return & (1 << 6) else "ns"
    return (f"{stack_register}_{security_suffix}", stack_register)


def _decode_flags(value: int | None, table: dict[int, tuple[str, str]]) -> list[tuple[str, str]]:
    """Decode active named bit flags from one system-register value."""
    if value is None:
        return []
    return [
        (name, description)
        for bit, (name, description) in sorted(table.items())
        if value & (1 << bit)
    ]


def _memory_region(address: int | None) -> str:
    """Classify an address using the standard Cortex-M memory map."""
    if address is None:
        return "?"
    if address < 0x1000:
        return "NULL-pointer / Vector Table"
    if address < 0x20000000:
        return "CODE (Flash / ROM)"
    if address < 0x40000000:
        return "SRAM"
    if address < 0x60000000:
        return "Peripherals (APB/AHB)"
    if address < 0x80000000:
        return "External RAM"
    if address < 0xA0000000:
        return "External Device"
    if address < 0xE0000000:
        return "System / Reserved"
    if address < 0xE0100000:
        return "PPB (SCB / NVIC / Core)"
    return "Vendor / Reserved"


def read_stacked_frame(
    reader: TargetMemory,
    lr_value: int,
    sp_value: int,
    sp_name: str,
) -> StackedFrame | str:
    """Decode an EXC_RETURN-selected basic or extended Cortex-M stack frame."""
    has_fpu = not bool(lr_value & (1 << 4))
    word_count = 26 if has_fpu else 8
    words: list[int] = []
    for index in range(word_count):
        address = sp_value + 4 * index
        try:
            words.append(reader.read_uint32(address))
        except (TargetReadError, ValueError) as error:
            return f"Cannot read stacked frame at 0x{address:08X}: {error}"

    core_offset = 18 if has_fpu else 0
    fp_registers = tuple(words[:16]) if has_fpu else None
    fpscr = words[16] if has_fpu else None
    s0_float = None
    if fp_registers is not None:
        s0_float = struct.unpack("<f", struct.pack("<I", fp_registers[0]))[0]
    return StackedFrame(
        lr_exc_return=lr_value,
        sp_name=sp_name,
        sp_address=sp_value,
        return_mode="Handler" if not lr_value & (1 << 3) else "Thread",
        target_domain="Secure" if not lr_value & 1 else "Non-Secure",
        has_fpu=has_fpu,
        secure_stacking=bool(lr_value & (1 << 6)),
        r0=words[core_offset],
        r1=words[core_offset + 1],
        r2=words[core_offset + 2],
        r3=words[core_offset + 3],
        r12=words[core_offset + 4],
        lr=words[core_offset + 5],
        pc=words[core_offset + 6],
        xpsr=words[core_offset + 7],
        fp_registers=fp_registers,
        fpscr=fpscr,
        s0_float=s0_float,
    )


class CortexMFaultCollector:
    """Collect the Cortex-M implementation of portable fault analysis."""

    architecture = Architecture.ARM
    service = DiagnosticServiceName.FAULT_ANALYSIS

    def supports(self, target: TargetDescription) -> bool:
        """Return whether this service recognizes a typed Cortex-M target."""
        return isinstance(target, CortexMTargetDescription) and target.core is not None

    def collect(
        self,
        reader: TargetMemory,
        target: TargetDescription,
        access: DiagnosticRuntimeAccess | None = None,
    ) -> DiagnosticReport:
        """Collect typed SCB fault state and the selected exception frame."""
        if not isinstance(target, CortexMTargetDescription) or target.core is None:
            raise ValueError("Cortex-M fault analysis requires a known core description")

        registers = access.registers if access is not None else None
        symbols = access.symbols if access is not None else None
        read_register = registers.read_first if registers is not None else lambda names: None
        pc = read_register(("pc", "r15"))
        lr = read_register(("lr", "r14"))
        xpsr = read_register(("xpsr",))
        ipsr_value = read_register(("ipsr",))
        ipsr = ipsr_value & 0x1FF if ipsr_value is not None else (xpsr or 0) & 0x1FF
        scb = read_scb(reader, target, cpuid_value=target.raw_cpuid)
        stacked_frame: StackedFrame | str | None = None
        if lr is not None and _is_exc_return(lr):
            uses_psp = bool(lr & (1 << 2))
            sp_name = "PSP" if uses_psp else "MSP"
            sp = read_register(_stack_register_names(lr, sp_name))
            if sp is None:
                sp = read_register(("sp", "r13"))
            if sp is None:
                stacked_frame = f"Cannot read stacked frame: {sp_name} is unavailable."
            else:
                stacked_frame = read_stacked_frame(reader, lr, sp, sp_name)

        resolve = symbols.resolve if symbols is not None else lambda address: "?"
        overview = self._overview_table(target, ipsr, pc, lr, xpsr, resolve)
        stack_table = self._stacked_frame_table(stacked_frame, resolve)
        scb_table = self._scb_table(scb)
        panels = self._diagnostic_panels(scb, stacked_frame, resolve)
        blocks: list[DiagnosticTable | DiagnosticPanel] = [overview]
        if stack_table is not None:
            blocks.append(stack_table)
        elif isinstance(stacked_frame, str):
            blocks.append(DiagnosticPanel("Stack Frame", (stacked_frame,)))
        blocks.extend((scb_table, *panels))
        return DiagnosticReport(
            self.service,
            target,
            tables=tuple(table for table in blocks if isinstance(table, DiagnosticTable)),
            panels=tuple(panel for panel in blocks if isinstance(panel, DiagnosticPanel)),
            blocks=tuple(blocks),
        )

    @staticmethod
    def _overview_table(
        target: CortexMTargetDescription,
        ipsr: int,
        pc: int | None,
        lr: int | None,
        xpsr: int | None,
        resolve: Callable[[int], str],
    ) -> DiagnosticTable:
        """Build the established overview as generic table data."""
        exception = _EXCEPTION_NAMES.get(ipsr, f"Interrupt #{ipsr}")
        exception_display = (
            f"{exception} (Exception #{ipsr})"
            if ipsr in (2, 3, 4, 5, 6, 7)
            else "None (Thread Mode)" if ipsr == 0 else f"{exception} (#{ipsr})"
        )
        rows: list[DiagnosticTableRow] = [
            DiagnosticTableRow(
                (
                    "Target Core",
                    f"{target.core_name} ({target.rnp_revision}) - {target.implementer_name}",
                )
            ),
            DiagnosticTableRow(("Active Exception", exception_display)),
            DiagnosticTableRow(
                (
                    "Execution Mode",
                    "HANDLER (in exception)" if ipsr else "THREAD (normal execution)",
                )
            ),
            DiagnosticTableRow(
                ("Current PC", f"0x{pc:08X} [{resolve(pc)}]" if pc is not None else "Unknown")
            ),
        ]
        if lr is None:
            rows.append(DiagnosticTableRow(("Current LR", "Unknown")))
        elif _is_exc_return(lr):
            rows.append(DiagnosticTableRow(("Current LR", f"0x{lr:08X} (valid EXC_RETURN)")))
        else:
            rows.append(DiagnosticTableRow(("Current LR", f"0x{lr:08X} [{resolve(lr)}]")))
        if xpsr is not None:
            rows.append(
                DiagnosticTableRow(
                    ("Current xPSR", f"0x{xpsr:08X} (IPSR={xpsr & 0x1FF}, T={(xpsr >> 24) & 1})")
                )
            )
        return DiagnosticTable("ARM Cortex-M Fault Overview", ("Property", "Value"), tuple(rows))

    @staticmethod
    def _stacked_frame_table(
        frame: StackedFrame | str | None,
        resolve: Callable[[int], str],
    ) -> DiagnosticTable | None:
        """Build stacked-frame table data only when hardware frame recovery succeeded."""
        if not isinstance(frame, StackedFrame):
            return None
        summary = (
            f"Stack: {frame.sp_name} @ 0x{frame.sp_address:08X} | "
            f"Return to: {frame.return_mode} ({frame.target_domain}) | "
            f"Frame: {'Extended (standard FPU)' if frame.has_fpu else 'Basic (8 registers)'}"
        )
        rows = [
            DiagnosticTableRow(("r0", f"0x{frame.r0:08X}", "")),
            DiagnosticTableRow(("r1", f"0x{frame.r1:08X}", "")),
            DiagnosticTableRow(("r2", f"0x{frame.r2:08X}", "")),
            DiagnosticTableRow(("r3", f"0x{frame.r3:08X}", "")),
            DiagnosticTableRow(("r12", f"0x{frame.r12:08X}", "")),
            DiagnosticTableRow(("lr", f"0x{frame.lr:08X}", f"Caller: {resolve(frame.lr)}")),
            DiagnosticTableRow(
                ("pc", f"0x{frame.pc:08X}", f"<-- Faulting instruction: {resolve(frame.pc)}")
            ),
            DiagnosticTableRow(
                (
                    "xpsr",
                    f"0x{frame.xpsr:08X}",
                    f"IPSR={frame.xpsr & 0x1FF}, T={(frame.xpsr >> 24) & 1} "
                    f"({'Thumb' if frame.xpsr & (1 << 24) else 'ARM (invalid on Cortex-M)'})",
                )
            ),
        ]
        if frame.fp_registers is not None:
            rows.extend(
                DiagnosticTableRow(
                    (
                        f"s{index}",
                        f"0x{value:08X}",
                        (
                            f"s0 (float32) = {frame.s0_float:.7g}"
                            if index == 0 and frame.s0_float is not None
                            else ""
                        ),
                    )
                )
                for index, value in enumerate(frame.fp_registers)
            )
        if frame.fpscr is not None:
            rows.append(
                DiagnosticTableRow(("fpscr", f"0x{frame.fpscr:08X}", "FPU Status & Control"))
            )
        return DiagnosticTable(
            f"Stacked frame at crash time ({summary})",
            ("Register", "Stacked Value", "Details / Symbol"),
            tuple(rows),
        )

    @staticmethod
    def _scb_table(scb: SystemRegisterSet) -> DiagnosticTable:
        """Build the SCB status table from typed architecture register results."""
        get: Callable[[str], int | None] = lambda name: CortexMFaultCollector._scb_value(scb, name)
        cfsr = get("CFSR")
        rows: list[DiagnosticTableRow] = []
        if cfsr is None:
            rows.append(
                DiagnosticTableRow(
                    ("CFSR", "Unavailable", "Not present on this target or memory inaccessible")
                )
            )
        else:
            mmfsr, bfsr, ufsr = cfsr & 0xFF, (cfsr >> 8) & 0xFF, (cfsr >> 16) & 0xFFFF
            flags = [
                f"{name}: {detail} (MemManage)"
                for name, detail in _decode_flags(mmfsr, _MMFSR_BITS)
            ]
            flags.extend(
                f"{name}: {detail} (BusFault)" for name, detail in _decode_flags(bfsr, _BFSR_BITS)
            )
            flags.extend(
                f"{name}: {detail} (UsageFault)" for name, detail in _decode_flags(ufsr, _UFSR_BITS)
            )
            rows.extend(
                (
                    DiagnosticTableRow(
                        (
                            "CFSR",
                            f"0x{cfsr:08X}",
                            "\n".join(flags) if flags else "No active error flags",
                        )
                    ),
                    DiagnosticTableRow(
                        (
                            " ├─ MMFSR",
                            f"0x{mmfsr:02X}",
                            f"{len(_decode_flags(mmfsr, _MMFSR_BITS))} active flag(s)",
                        )
                    ),
                    DiagnosticTableRow(
                        (
                            " ├─ BFSR",
                            f"0x{bfsr:02X}",
                            f"{len(_decode_flags(bfsr, _BFSR_BITS))} active flag(s)",
                        )
                    ),
                    DiagnosticTableRow(
                        (
                            " └─ UFSR",
                            f"0x{ufsr:04X}",
                            f"{len(_decode_flags(ufsr, _UFSR_BITS))} active flag(s)",
                        )
                    ),
                )
            )
        status_registers: tuple[tuple[str, dict[int, tuple[str, str]], str], ...] = (
            ("HFSR", _HFSR_BITS, "No active error flags"),
            ("DFSR", _DFSR_BITS, "No debug event"),
        )
        for name, table, empty in status_registers:
            value = get(name)
            if value is not None:
                decoded_flags = _decode_flags(value, table)
                rows.append(
                    DiagnosticTableRow(
                        (
                            name,
                            f"0x{value:08X}",
                            "\n".join(f"{key}: {text}" for key, text in decoded_flags) or empty,
                        )
                    )
                )
        mmfar, bfar = get("MMFAR"), get("BFAR")
        if mmfar is not None:
            rows.append(
                DiagnosticTableRow(
                    (
                        "MMFAR",
                        f"0x{mmfar:08X}",
                        f"{'[VALID]' if cfsr is not None and cfsr & 0x80 else '[INVALID]'} "
                        f"Region: {_memory_region(mmfar)}",
                    )
                )
            )
        if bfar is not None:
            rows.append(
                DiagnosticTableRow(
                    (
                        "BFAR",
                        f"0x{bfar:08X}",
                        f"{'[VALID]' if cfsr is not None and cfsr & (1 << 15) else '[INVALID]'} "
                        f"Region: {_memory_region(bfar)}",
                    )
                )
            )
        shcsr = get("SHCSR")
        if shcsr is not None:
            enabled = [
                name
                for bit, name in (
                    (16, "MemManage"),
                    (17, "BusFault"),
                    (18, "UsageFault"),
                    (19, "SecureFault"),
                )
                if shcsr & (1 << bit)
            ]
            rows.append(
                DiagnosticTableRow(
                    (
                        "SHCSR",
                        f"0x{shcsr:08X}",
                        "Enabled configurable handlers: "
                        + (
                            ", ".join(enabled)
                            if enabled
                            else "None (direct escalation to HardFault)"
                        ),
                    )
                )
            )
        vtor = get("VTOR")
        if vtor is not None:
            rows.append(
                DiagnosticTableRow(
                    ("VTOR", f"0x{vtor:08X}", f"Vector table @ {_memory_region(vtor)}")
                )
            )
        sfsr = get("SFSR")
        if sfsr is not None and sfsr not in (0, 0xFFFFFFFF):
            decoded_flags = _decode_flags(sfsr, _SFSR_BITS)
            rows.append(
                DiagnosticTableRow(
                    (
                        "SFSR",
                        f"0x{sfsr:08X}",
                        "\n".join(f"{key}: {text}" for key, text in decoded_flags),
                    )
                )
            )
            sfar = get("SFAR")
            if sfar is not None and sfsr & (1 << 6):
                rows.append(
                    DiagnosticTableRow(
                        ("SFAR", f"0x{sfar:08X}", f"[VALID] Region: {_memory_region(sfar)}")
                    )
                )
        return DiagnosticTable(
            "SCB (System Control Block) Status Registers",
            ("Register", "Value", "Active Flags & Meaning"),
            tuple(rows),
        )

    @staticmethod
    def _diagnostic_panels(
        scb: SystemRegisterSet,
        frame: StackedFrame | str | None,
        resolve: Callable[[int], str],
    ) -> tuple[DiagnosticPanel, ...]:
        """Derive ordered probable-cause lines from SCB fault information."""
        get: Callable[[str], int | None] = lambda name: CortexMFaultCollector._scb_value(scb, name)
        cfsr, hfsr = get("CFSR") or 0, get("HFSR") or 0
        bfar, mmfar, cpacr = get("BFAR"), get("MMFAR"), get("CPACR")
        mmfsr, bfsr, ufsr = cfsr & 0xFF, (cfsr >> 8) & 0xFF, (cfsr >> 16) & 0xFFFF
        lines: list[str] = []
        if hfsr & (1 << 30):
            source = (
                "BusFault"
                if bfsr
                else "MemManage Fault" if mmfsr else "An UsageFault" if ufsr else None
            )
            lines.append(
                "• HardFault escalation: "
                + (
                    f"A {source} was forced to HardFault (source handler disabled in SHCSR)."
                    if source is not None
                    else "Forced HardFault without configurable cause bits (handler masked by PRIMASK/FAULTMASK)."
                )
            )
        if bfsr & (1 << 1):
            if bfsr & (1 << 7) and bfar is not None:
                if bfar < 0x1000:
                    lines.append(
                        f"• NULL pointer dereference: Invalid memory access at 0x{bfar:08X} ({_memory_region(bfar)})."
                    )
                elif 0x40000000 <= bfar < 0x60000000:
                    lines.append(
                        f"• Peripheral bus error (0x{bfar:08X}): Verify that the peripheral clock (RCC/PCLK) is enabled before any access."
                    )
                else:
                    lines.append(
                        f"• Precise BusFault at 0x{bfar:08X}: Invalid memory access in {_memory_region(bfar)} region."
                    )
        elif bfsr & (1 << 2):
            lines.append(
                "• Imprecise (asynchronous) BusFault: Caused by a write buffer. The stacked PC is downstream of the actual faulting access. To locate the exact access, temporarily disable write buffering (e.g. ACTLR.DISDEFWBUF)."
            )
        if bfsr & 1:
            lines.append(
                "• Instruction Fetch BusFault: Attempted execution from an invalid or inaccessible memory region (corrupted function pointer, overwritten vtable)."
            )
        if bfsr & (1 << 4):
            lines.append(
                "• Bus error during Stacking: The stack (MSP/PSP) overflowed or points to invalid memory."
            )
        if bfsr & (1 << 3):
            lines.append("• Bus error during Unstacking: Corrupted stack upon exception return.")
        if mmfsr & (1 << 1):
            address = f" at 0x{mmfar:08X}" if mmfsr & (1 << 7) and mmfar is not None else ""
            lines.append(
                f"• MPU violation on data{address}: Access prohibited by MPU region permissions."
            )
        if mmfsr & 1:
            lines.append(
                "• MPU violation on instruction: Attempted execution in an MPU region marked eXecute-Never (XN)."
            )
        for bit, line in (
            (
                0,
                "• Undefined instruction (UNDEFINSTR): Unknown or corrupted opcode (branching into data/NULL or incorrect instruction alignment).",
            ),
            (
                1,
                "• Invalid Thumb state (INVSTATE): Branch to an even address (bit T=0). On Cortex-M, function pointers must have the least significant bit (LSB) set to 1.",
            ),
            (
                2,
                "• Invalid EXC_RETURN (INVPC): Illegal exception return value loaded into PC (LR or stack corruption).",
            ),
            (
                4,
                "• Hardware stack overflow (STKOF): Stack pointer exceeded configured limit (MSPLIM/PSPLIM).",
            ),
            (8, "• Unaligned access (UNALIGNED): Unaligned access trapped by CCR.UNALIGN_TRP."),
            (9, "• Divide by zero (DIVBYZERO): Integer division by zero trapped by CCR.DIV_0_TRP."),
        ):
            if ufsr & (1 << bit):
                lines.append(line)
        if ufsr & (1 << 3):
            lines.append(
                "• Disabled FPU coprocessor (NOCP): FPU instruction executed while FPU is disabled. Add SCB->CPACR |= (0xF << 20); during initialization."
                if cpacr is not None and cpacr & 0x00F00000 != 0x00F00000
                else "• Unimplemented coprocessor access (NOCP): Access to a non-existent or unconfigured coprocessor."
            )
        if hfsr & (1 << 1):
            lines.append(
                "• Vector Table read error (VECTTBL): Vector table is unreadable. Verify VTOR register configuration and Flash memory."
            )
        if isinstance(frame, StackedFrame):
            lines.append(
                f"• Crash location: Instruction at 0x{frame.pc:08X} ({resolve(frame.pc)}), called from 0x{frame.lr:08X} ({resolve(frame.lr)})."
            )
        if not lines:
            lines.append("• No obvious error conditions detected in SCB registers.")
        return (DiagnosticPanel("Diagnostics & Probable Causes", tuple(lines)),)

    @staticmethod
    def _scb_value(scb: SystemRegisterSet, name: str) -> int | None:
        """Return the available value of a typed SCB register."""
        register = scb.get(name)
        return register.value if register is not None else None
