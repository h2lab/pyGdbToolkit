# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""The ``lscpu`` GDB command for Cortex-M and AArch64 targets."""

from __future__ import annotations

import gdb
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .arch import Architecture
from .arch.aarch64.cpu import CpuReport, affinity, physical_address_bits, simd_support
from .arch.aarch64.session_state import cpu_aarch64_report
from .arch.arm.coresight import RomTableDiscovery
from .arch.arm.models import DeviceReport, FieldValue
from .arch.arm.session_state import cpu_arm_report
from .arch.trace import TraceCapabilities, TraceComponentKind
from .session import SESSION, CommandHelp, CommandUsage
from .target_memory import TargetReadError

CONSOLE = Console(force_terminal=True)


class LscpuCmd(gdb.Command):
    """Display architectural CPU identity and available device information."""

    HELP = CommandHelp(
        name="lscpu",
        summary="Identify the CPU and report available architectural or device information.",
        usage=(CommandUsage("lscpu", "Display the CPU and electronic-signature report"),),
    )

    def __init__(self) -> None:
        """Register the command with GDB."""
        super().__init__("lscpu", gdb.COMMAND_USER)
        SESSION.register_command(self.HELP)

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Run the command.

        Parameters
        ----------
        arg
            The user-supplied argument string. This command accepts no arguments.
        from_tty
            Whether GDB invoked the command from its terminal.

        Raises
        ------
        gdb.GdbError
            If arguments are supplied or the target CPUID cannot be read.
        """
        del from_tty
        if arg.strip():
            raise gdb.GdbError("lscpu does not accept arguments")

        try:
            render_report()
        except TargetReadError as error:
            raise gdb.GdbError(str(error)) from error


def cpu_report() -> DeviceReport | CpuReport:
    """Collect the CPU report through the implementation for the session architecture."""
    match SESSION.architecture:
        case Architecture.ARM:
            return cpu_arm_report()
        case Architecture.AARCH64:
            return cpu_aarch64_report()
        case None:
            reason = SESSION.probe().unavailable_reason or "target architecture unavailable"
            raise gdb.GdbError(f"lscpu: target architecture unavailable: {reason}")
        case unsupported_architecture:
            raise gdb.GdbError(f"lscpu does not support architecture '{unsupported_architecture}'")


def render_report() -> None:
    """Render the architecture-specific result of the common CPU collection API."""
    report = cpu_report()
    if isinstance(report, DeviceReport):
        render_arm_report(report, SESSION.trace_capabilities())
    else:
        render_aarch64_report(report, trace=None)


def render_aarch64_report(report: CpuReport, trace: TraceCapabilities | None = None) -> None:
    """Render architected CPU identity without inferring SoC signatures or topology."""
    table = Table(
        title="AArch64 CPU report",
        box=box.SIMPLE_HEAVY,
        header_style="bold cyan",
        show_header=True,
    )
    table.add_column("Property", style="bold", no_wrap=True)
    table.add_column("Value")
    table.add_row("Architecture", report.target.gdb_architecture)
    identity = report.identity
    midr = report.register("MIDR_EL1")
    missing_identity = Text(f"Unavailable: {midr.unavailable_reason}", style="yellow")
    table.add_row("Core type", missing_identity if identity is None else identity.core_name)
    table.add_row("Core revision", missing_identity if identity is None else identity.rnp_revision)
    table.add_row(
        "Implementer", missing_identity if identity is None else identity.implementer_name
    )
    table.add_row(
        "CPU part number", missing_identity if identity is None else f"0x{identity.part_number:03X}"
    )
    mpidr = report.register("MPIDR_EL1").value
    if mpidr is not None:
        table.add_row(
            "Affinity (Aff3:Aff2:Aff1:Aff0)", ":".join(str(part) for part in affinity(mpidr))
        )
        table.add_row("MPIDR MT / U", f"{(mpidr >> 24) & 1} / {(mpidr >> 30) & 1}")
    current_el = report.register("CurrentEL").value
    if current_el is not None:
        table.add_row("Current exception level", f"EL{(current_el >> 2) & 3}")
    pfr0 = report.register("ID_AA64PFR0_EL1").value
    if pfr0 is not None:
        table.add_row("Floating point", simd_support(pfr0, 16))
        table.add_row("Advanced SIMD", simd_support(pfr0, 20))
    mmfr0 = report.register("ID_AA64MMFR0_EL1").value
    if mmfr0 is not None:
        bits = physical_address_bits(mmfr0)
        table.add_row(
            "Physical address width", "Unknown encoding" if bits is None else f"{bits} bits"
        )
    ctr = report.register("CTR_EL0").value
    if ctr is not None:
        table.add_row("Minimum instruction cache line", f"{4 << (ctr & 0xF)} bytes")
        table.add_row("Minimum data cache line", f"{4 << ((ctr >> 16) & 0xF)} bytes")
    for register in report.registers:
        value: str | Text
        if register.value is None:
            value = Text(f"Unavailable: {register.unavailable_reason}", style="yellow")
        else:
            unknown_digits = "?" * ((register.width_bits - register.valid_bits) // 4)
            value = (
                f"0x{unknown_digits}{register.value:0{register.valid_bits // 4}X}"
                f" ({register.source})"
            )
        table.add_row(register.name, value)
    CONSOLE.print(table)


def render_arm_report(report: DeviceReport, trace: TraceCapabilities | None = None) -> None:
    """Render a stable device report through the shared Rich console.

    Parameters
    ----------
    report
        Decoded CPU and device information.
    trace
        Optional architecture-neutral trace capability evidence.
    """
    table = Table(
        title="Cortex-M CPU report",
        box=box.SIMPLE_HEAVY,
        header_style="bold cyan",
        show_header=True,
    )
    table.add_column("Property", style="bold", no_wrap=True)
    table.add_column("Value")
    table.add_row("Core type", report.target.core_name)
    table.add_row("Core revision", report.target.rnp_revision)
    table.add_row("Implementer", report.target.implementer_name)
    table.add_row(
        "MCU ROM JEP106 identity",
        _field_text(_rom_jep106_field(report.discovery.mcu_rom)),
    )
    table.add_row("Vendor", report.vendor)
    table.add_row("Product line", _field_text(report.product_line))
    table.add_row("Part number", _field_text(report.part_number))
    table.add_row("RAM", _field_text(report.ram))
    table.add_row("Flash", _field_text(report.flash))
    table.add_row("Package type", _field_text(report.package))
    table.add_row("Serial number", _field_text(report.serial_number))
    if trace is not None:
        table.add_section()
        for kind in TraceComponentKind:
            table.add_row(kind.value.upper(), _trace_text(trace, kind))
    CONSOLE.print(table)


def _trace_text(trace: TraceCapabilities, kind: TraceComponentKind) -> Text:
    """Format all components of one kind without promoting unknown properties."""
    if not trace.is_available:
        return Text(f"Unavailable: {trace.unavailable_reason}", style="yellow")
    components = tuple(component for component in trace.components if component.kind == kind)
    if not components:
        return Text("Not detected")
    details: list[str] = []
    for component in components:
        details.append(f"Detected at 0x{component.base:08X}")
        if kind == TraceComponentKind.ETM:
            details.append(f"Architecture version: {component.version or 'Unknown'}")
            security = component.security_filtering
            filtering = (
                "Unknown" if security is None else ("Supported" if security else "Not supported")
            )
            details.append(f"Secure/non-Secure filtering: {filtering}")
            if component.secure_exception_levels is not None:
                details.append(f"EXLEVEL_S mask: 0x{component.secure_exception_levels:X}")
            if component.nonsecure_exception_levels is not None:
                details.append(f"EXLEVEL_NS mask: 0x{component.nonsecure_exception_levels:X}")
        if kind in (TraceComponentKind.ETB, TraceComponentKind.ETF):
            size = component.buffer_size_bytes
            details.append(f"Buffer size: {'Unknown' if size is None else f'{size:,} bytes'}")
        for register in component.registers:
            if not register.is_available:
                details.append(f"{register.name} unavailable: {register.unavailable_reason}")
    return Text("\n".join(details))


def _field_text(field: FieldValue) -> Text:
    """Create a Rich value cell with a visible unavailable-state reason.

    Parameters
    ----------
    field
        A ``FieldValue`` supplied by the report model.

    Returns
    -------
    Text
        The formatted field cell.
    """
    display = field.display()
    if field.is_available:
        return Text(display)
    return Text.assemble(("Unavailable: ", "yellow"), display.removeprefix("Unavailable: "))


def _rom_jep106_field(discovery: RomTableDiscovery) -> FieldValue:
    """Format a table root's validated JEP106 identity when it is available."""
    if discovery.table is None:
        assert discovery.unavailable_reason is not None
        return FieldValue.unavailable(discovery.unavailable_reason)
    identity = discovery.table.identity.peripheral_id.jep106
    if identity is None:
        return FieldValue.unavailable("ROM-table root does not advertise a JEDEC manufacturer")
    return FieldValue.known(identity.display())
