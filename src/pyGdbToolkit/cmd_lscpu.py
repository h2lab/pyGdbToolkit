# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""The ``lscpu`` GDB command for Cortex-M targets."""

from __future__ import annotations

import gdb
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .cpuid import CPUID_ADDRESS, decode_cpuid
from .coresight import RomTableDiscovery, discover_rom_tables
from .models import DeviceReport, FieldValue
from .providers import DEFAULT_PROVIDER_REGISTRY
from .target_memory import TargetMemoryReader, TargetReadError

CONSOLE = Console(force_terminal=True)


class LscpuCmd(gdb.Command):
    """Display CPU and electronic-signature information for a Cortex-M target."""

    def __init__(self) -> None:
        """Register the command with GDB."""
        super().__init__("lscpu", gdb.COMMAND_USER)

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
            reader = TargetMemoryReader()
            cpuid = decode_cpuid(reader.read_uint32(CPUID_ADDRESS))
        except TargetReadError as error:
            raise gdb.GdbError(str(error)) from error

        discovery = discover_rom_tables(reader)
        render_report(DEFAULT_PROVIDER_REGISTRY.inspect(reader, cpuid, discovery))


def render_report(report: DeviceReport) -> None:
    """Render a stable device report through the shared Rich console.

    Parameters
    ----------
    report
        Decoded CPU and device information.
    """
    table = Table(
        title="Cortex-M CPU report",
        box=box.SIMPLE_HEAVY,
        header_style="bold cyan",
        show_header=True,
    )
    table.add_column("Property", style="bold", no_wrap=True)
    table.add_column("Value")
    table.add_row("Core type", report.cpuid.core)
    table.add_row("Core revision", report.cpuid.rnp_revision)
    table.add_row("Implementer", report.cpuid.implementer_name)
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
    CONSOLE.print(table)


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


def _rom_table_field(discovery: RomTableDiscovery) -> FieldValue:
    """Format one ROM-table discovery result for the report.

    Parameters
    ----------
    discovery
        The best-effort scan result for one fixed ROM-table root.

    Returns
    -------
    FieldValue
        The table base and number of reachable components, or its failure reason.
    """
    if discovery.table is None:
        assert discovery.unavailable_reason is not None
        return FieldValue.unavailable(discovery.unavailable_reason)
    component_count = len(discovery.table.components)
    return FieldValue.known(
        f"0x{discovery.base:08X} ({component_count} discovered component(s))"
    )


def _rom_jep106_field(discovery: RomTableDiscovery) -> FieldValue:
    """Format a table root's validated JEP106 identity when it is available."""
    if discovery.table is None:
        assert discovery.unavailable_reason is not None
        return FieldValue.unavailable(discovery.unavailable_reason)
    identity = discovery.table.identity.peripheral_id.jep106
    if identity is None:
        return FieldValue.unavailable("ROM-table root does not advertise a JEDEC manufacturer")
    return FieldValue.known(identity.display())


def _rom_part_field(discovery: RomTableDiscovery) -> FieldValue:
    """Format a table root's 12-bit component part number when it is available."""
    if discovery.table is None:
        assert discovery.unavailable_reason is not None
        return FieldValue.unavailable(discovery.unavailable_reason)
    return FieldValue.known(f"0x{discovery.table.identity.peripheral_id.part_number:03X}")


def _rom_components_field(discovery: RomTableDiscovery) -> FieldValue:
    """Format validated component identities as compact diagnostic information."""
    if discovery.table is None:
        assert discovery.unavailable_reason is not None
        return FieldValue.unavailable(discovery.unavailable_reason)
    if not discovery.table.components:
        return FieldValue.known("none")
    components = ", ".join(
        (
            f"0x{component.base:08X} "
            f"(class 0x{component.component_id.component_class:X}, "
            f"part 0x{component.peripheral_id.part_number:03X})"
        )
        for component in discovery.table.components
    )
    return FieldValue.known(components)
