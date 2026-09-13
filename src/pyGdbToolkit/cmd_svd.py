"""The ``svd`` GDB command for CMSIS-SVD inspection, manipulation, and monitoring."""

from __future__ import annotations

from dataclasses import dataclass, field
import glob
import json
import os
from pathlib import Path
import shlex
from typing import Any

import gdb
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .cpuid import CPUID_ADDRESS, decode_cpuid
from .providers import DEFAULT_PROVIDER_REGISTRY
from .svd import (
    SvdDevice,
    SvdError,
    SvdNotFoundError,
    SvdPeripheral,
    SvdRegister,
    parse_svd_file,
    resolve_and_fetch_svd,
)
from .target_memory import TargetMemoryReader, TargetReadError

CONSOLE = Console(force_terminal=True)


@dataclass
class SvdSessionState:
    """Encapsulate the active SVD device model and dictionary in the GDB session."""

    device: SvdDevice | None = None
    svd_dict: dict[str, Any] | None = None
    svd_path: Path | None = None
    watchpoints: list[SvdWatchpoint] = field(default_factory=list)


SESSION = SvdSessionState()


def _get_c_type_for_size(size_bits: int) -> str:
    """Return the canonical C integer type name matching a register bit width.

    Parameters
    ----------
    size_bits : int
        Register size in bits.

    Returns
    -------
    str
        C type identifier.
    """
    if size_bits <= 8:
        return "unsigned char"
    if size_bits <= 16:
        return "unsigned short"
    if size_bits <= 32:
        return "unsigned int"
    return "unsigned long long"


def _parse_numeric_value(val_str: str) -> int:
    """Parse a numeric string supporting hex, binary, and decimal formats.

    Parameters
    ----------
    val_str : str
        Numeric representation.

    Returns
    -------
    int
        Decoded integer value.
    """
    val_str = val_str.strip()
    if val_str.startswith("#"):
        return int(val_str[1:], 16)
    if val_str.startswith(("0x", "0X")):
        return int(val_str, 16)
    if val_str.startswith(("0b", "0B")):
        return int(val_str, 2)
    return int(val_str, 10)


def _read_register_value(reader: TargetMemoryReader, base_address: int, reg: SvdRegister) -> int:
    """Read a register value from target memory.

    Parameters
    ----------
    reader : TargetMemoryReader
        Target memory reader.
    base_address : int
        Base physical address of the peripheral.
    reg : SvdRegister
        Register definition.

    Returns
    -------
    int
        The raw integer value read from memory.
    """
    reg_addr = base_address + reg.address_offset
    size_bytes = max(1, (reg.size + 7) // 8)
    raw_bytes = reader.read_bytes(reg_addr, size_bytes)
    return int.from_bytes(raw_bytes, byteorder="little")


def _write_register_value(base_address: int, reg: SvdRegister, value: int) -> None:
    """Write an integer value into a target register.

    Parameters
    ----------
    base_address : int
        Base physical address of the peripheral.
    reg : SvdRegister
        Register definition.
    value : int
        Value to write.
    """
    reg_addr = base_address + reg.address_offset
    size_bytes = max(1, (reg.size + 7) // 8)
    val_bytes = value.to_bytes(size_bytes, byteorder="little")
    inferior = gdb.selected_inferior()
    inferior.write_memory(reg_addr, val_bytes)


class SvdWatchpoint(gdb.Breakpoint):
    """A hardware or software watchpoint tracking modifications to an SVD register."""

    def __init__(
        self,
        spec: str,
        peripheral_name: str,
        register_name: str,
        register: SvdRegister,
        address: int,
        initial_value: int,
    ) -> None:
        super().__init__(spec, type=gdb.BP_WATCHPOINT, wp_class=gdb.WP_WRITE)
        self.peripheral_name = peripheral_name
        self.register_name = register_name
        self.register = register
        self.address = address
        self.last_value = initial_value

    def stop(self) -> bool:
        """Handle watchpoint triggers and notify the user with decoded bitfield changes.

        Returns
        -------
        bool
            Always True to halt execution and return control to the GDB prompt.
        """
        new_val = self.last_value
        try:
            reader = TargetMemoryReader()
            size_bytes = max(1, (self.register.size + 7) // 8)
            raw_bytes = reader.read_bytes(self.address, size_bytes)
            new_val = int.from_bytes(raw_bytes, byteorder="little")
        except TargetReadError:
            pass

        render_watchpoint_hit(
            self.peripheral_name,
            self.register_name,
            self.register,
            self.address,
            self.last_value,
            new_val,
        )
        self.last_value = new_val
        return True


def render_watchpoint_hit(
    peripheral_name: str,
    register_name: str,
    register: SvdRegister,
    address: int,
    old_value: int,
    new_value: int,
) -> None:
    """Render a Rich notification when an SVD register watchpoint triggers.

    Parameters
    ----------
    peripheral_name : str
        Name of the parent peripheral.
    register_name : str
        Name of the register.
    register : SvdRegister
        Register model definition.
    address : int
        Physical address of the register.
    old_value : int
        Value before write.
    new_value : int
        Value after write.
    """
    hex_len = max(2, (register.size + 3) // 4)
    old_hex = f"0x{old_value:0{hex_len}X}"
    new_hex = f"0x{new_value:0{hex_len}X}"

    title_text = Text.assemble(
        ("SVD Watchpoint Hit: ", "bold yellow"),
        (f"{peripheral_name}->{register_name}", "bold cyan"),
        (f" @ 0x{address:08X}", "bold magenta"),
    )

    table = Table(
        title=title_text,
        box=box.SIMPLE_HEAVY,
        header_style="bold cyan",
        show_header=True,
    )
    table.add_column("Property", style="bold", no_wrap=True)
    table.add_column("Value")
    table.add_row("Previous Value", old_hex)
    table.add_row("New Value", Text(new_hex, style="bold green"))
    table.add_row("Difference", f"0x{(old_value ^ new_value):0{hex_len}X}")

    CONSOLE.print(table)

    if register.fields:
        field_table = Table(
            title="Modified Bitfields",
            box=box.SIMPLE_HEAVY,
            header_style="bold cyan",
            show_header=True,
        )
        field_table.add_column("Bits", style="cyan", no_wrap=True)
        field_table.add_column("Field", style="bold", no_wrap=True)
        field_table.add_column("Old Value", style="yellow")
        field_table.add_column("New Value", style="bold green")
        field_table.add_column("Description")

        diff_count = 0
        for field_obj in register.fields:
            old_field_val = field_obj.extract_value(old_value)
            new_field_val = field_obj.extract_value(new_value)
            if old_field_val != new_field_val:
                diff_count += 1
                if field_obj.bit_width == 1:
                    bits_str = f"[{field_obj.bit_offset}]"
                else:
                    msb = field_obj.bit_offset + field_obj.bit_width - 1
                    bits_str = f"[{msb}:{field_obj.bit_offset}]"

                f_hex_len = max(1, (field_obj.bit_width + 3) // 4)
                field_table.add_row(
                    bits_str,
                    field_obj.name,
                    f"0x{old_field_val:0{f_hex_len}X}",
                    f"0x{new_field_val:0{f_hex_len}X}",
                    field_obj.description or "--",
                )

        if diff_count > 0:
            CONSOLE.print(field_table)


def render_load_success(
    device: SvdDevice,
    svd_filename: str,
    cache_path: Path,
    vendor: str | None,
    product_line: str | None,
) -> None:
    """Render a summary table for a successfully detected and loaded SVD file.

    Parameters
    ----------
    device : SvdDevice
        Parsed device object.
    svd_filename : str
        Matched SVD filename.
    cache_path : Path
        Local file path to the SVD XML.
    vendor : str | None
        Target vendor identifier.
    product_line : str | None
        Target product line name.
    """
    table = Table(
        title="SVD Target Auto-Detection & Load",
        box=box.SIMPLE_HEAVY,
        header_style="bold cyan",
        show_header=True,
    )
    table.add_column("Property", style="bold", no_wrap=True)
    table.add_column("Value")
    if vendor:
        table.add_row("Detected Vendor", vendor)
    if product_line:
        table.add_row("Product Line", product_line)
    table.add_row("Matched SVD", Text(svd_filename, style="bold green"))
    table.add_row("Device Name", device.name)
    if device.version:
        table.add_row("SVD Schema Version", device.version)
    table.add_row("Peripherals Loaded", str(len(device.peripherals)))
    table.add_row("Local Cache Path", str(cache_path))
    CONSOLE.print(table)


def render_file_load_success(device: SvdDevice, file_path: Path) -> None:
    """Render a summary table for an explicitly loaded SVD file.

    Parameters
    ----------
    device : SvdDevice
        Parsed SVD device.
    file_path : Path
        Path to the loaded SVD file.
    """
    table = Table(
        title="SVD File Loaded Successfully",
        box=box.SIMPLE_HEAVY,
        header_style="bold cyan",
        show_header=True,
    )
    table.add_column("Property", style="bold", no_wrap=True)
    table.add_column("Value")
    table.add_row("File Path", str(file_path))
    table.add_row("Device Name", Text(device.name, style="bold green"))
    if device.vendor:
        table.add_row("Vendor", device.vendor)
    if device.version:
        table.add_row("Version", device.version)
    table.add_row("Peripherals Loaded", str(len(device.peripherals)))
    CONSOLE.print(table)


def render_peripheral_state(peripheral: SvdPeripheral, reader: TargetMemoryReader) -> None:
    """Render the canonical status of all registers in a peripheral.

    Parameters
    ----------
    peripheral : SvdPeripheral
        Target peripheral model.
    reader : TargetMemoryReader
        Memory reader instance.
    """
    title_text = f"Peripheral: {peripheral.name} @ 0x{peripheral.base_address:08X}" + (
        f" ({peripheral.description})" if peripheral.description else ""
    )
    table = Table(
        title=title_text,
        box=box.SIMPLE_HEAVY,
        header_style="bold cyan",
        show_header=True,
    )
    table.add_column("Offset", style="cyan", no_wrap=True)
    table.add_column("Register", style="bold", no_wrap=True)
    table.add_column("Address", style="magenta", no_wrap=True)
    table.add_column("Size", no_wrap=True)
    table.add_column("Access", no_wrap=True)
    table.add_column("Reset Value", no_wrap=True)
    table.add_column("Current Value", style="bold")
    table.add_column("Description")

    for reg in peripheral.registers:
        reg_addr = peripheral.base_address + reg.address_offset
        hex_len = max(2, (reg.size + 3) // 4)
        reset_str = f"0x{reg.reset_value:0{hex_len}X}" if reg.reset_value is not None else "--"
        access_str = reg.access or "--"
        size_str = f"{reg.size}-bit"

        try:
            val = _read_register_value(reader, peripheral.base_address, reg)
            val_str = Text(f"0x{val:0{hex_len}X}", style="bold green")
        except TargetReadError:
            val_str = Text("[Read Error]", style="bold yellow")

        table.add_row(
            f"+0x{reg.address_offset:04X}",
            reg.name,
            f"0x{reg_addr:08X}",
            size_str,
            access_str,
            reset_str,
            val_str,
            reg.description or "--",
        )

    CONSOLE.print(table)


def render_register_detail(peripheral: SvdPeripheral, reg: SvdRegister, value: int) -> None:
    """Render the detailed bitfield view for a specific register.

    Parameters
    ----------
    peripheral : SvdPeripheral
        Parent peripheral model.
    reg : SvdRegister
        Register model.
    value : int
        Current register value read from target memory.
    """
    reg_addr = peripheral.base_address + reg.address_offset
    hex_len = max(2, (reg.size + 3) // 4)
    raw_hex = f"0x{value:0{hex_len}X}"
    bin_str = f"0b{value:0{reg.size}b}"

    summary_table = Table(
        title=f"Register: {peripheral.name}->{reg.name}",
        box=box.SIMPLE_HEAVY,
        header_style="bold cyan",
        show_header=True,
    )
    summary_table.add_column("Property", style="bold", no_wrap=True)
    summary_table.add_column("Value")
    summary_table.add_row("Peripheral", f"{peripheral.name} (Base 0x{peripheral.base_address:08X})")
    summary_table.add_row("Register Address", f"0x{reg_addr:08X} (+0x{reg.address_offset:04X})")
    summary_table.add_row(
        "Current Value", Text(f"{raw_hex} ({value}, {bin_str})", style="bold green")
    )
    if reg.reset_value is not None:
        summary_table.add_row("Reset Value", f"0x{reg.reset_value:0{hex_len}X}")
    summary_table.add_row("Access Rights", reg.access or "unspecified")
    if reg.description:
        summary_table.add_row("Description", reg.description)
    CONSOLE.print(summary_table)

    if not reg.fields:
        CONSOLE.print(
            Panel(
                Text(
                    "No bitfield definitions available in SVD for this register.",
                    style="dim italic",
                ),
                box=box.ROUNDED,
            )
        )
        return

    fields_table = Table(
        title=f"Bitfields for {peripheral.name}->{reg.name}",
        box=box.SIMPLE_HEAVY,
        header_style="bold cyan",
        show_header=True,
    )
    fields_table.add_column("Bits", style="cyan", no_wrap=True)
    fields_table.add_column("Field", style="bold", no_wrap=True)
    fields_table.add_column("Access", no_wrap=True)
    fields_table.add_column("Value (Hex)", style="bold green", no_wrap=True)
    fields_table.add_column("Value (Dec/Bin)", no_wrap=True)
    fields_table.add_column("Reset", no_wrap=True)
    fields_table.add_column("Description")

    for field_obj in sorted(reg.fields, key=lambda f: f.bit_offset, reverse=True):
        extracted = field_obj.extract_value(value)
        f_hex_len = max(1, (field_obj.bit_width + 3) // 4)
        f_hex = f"0x{extracted:0{f_hex_len}X}"
        f_bin = f"0b{extracted:0{field_obj.bit_width}b}"
        f_dec_bin = f"{extracted} ({f_bin})" if field_obj.bit_width > 1 else str(extracted)

        if field_obj.bit_width == 1:
            bits_str = f"[{field_obj.bit_offset}]"
        else:
            msb = field_obj.bit_offset + field_obj.bit_width - 1
            bits_str = f"[{msb}:{field_obj.bit_offset}]"

        reset_str = (
            f"0x{field_obj.reset_value:0{f_hex_len}X}"
            if field_obj.reset_value is not None
            else "--"
        )

        fields_table.add_row(
            bits_str,
            field_obj.name,
            field_obj.access or "--",
            f_hex,
            f_dec_bin,
            reset_str,
            field_obj.description or "--",
        )

    CONSOLE.print(fields_table)


def render_help() -> None:
    """Render the command overview and help table."""
    table = Table(
        title="CMSIS-SVD Target Inspection & Control Commands",
        box=box.SIMPLE_HEAVY,
        header_style="bold cyan",
        show_header=True,
    )
    table.add_column("Command Syntax", style="bold yellow", no_wrap=True)
    table.add_column("Description")
    table.add_row(
        "svd load", "Auto-detect target CPUID/SoC, fetch and load matching SVD definition"
    )
    table.add_row("svd read <file.svd>", "Load an explicit SVD XML file from local filesystem")
    table.add_row(
        "svd show <device>", "Display canonical memory state and registers of a peripheral"
    )
    table.add_row(
        "svd show <device> <register>", "Display detailed register state and decoded bitfields"
    )
    table.add_row(
        "svd write <device> <reg> <hex_val>", "Write numeric/hex value into peripheral register"
    )
    table.add_row(
        "svd monitor <device> <reg>", "Set a watchpoint on a register and report bitfield diffs"
    )
    table.add_row(
        "svd dump <device> <file.json>", "Dump snapshot of device/peripheral state into a JSON file"
    )
    table.add_row("svd help", "Show this command reference")
    CONSOLE.print(table)


class SvdCmd(gdb.Command):
    """Inspect, manipulate, and monitor hardware registers using CMSIS-SVD definitions."""

    def __init__(self) -> None:
        super().__init__("svd", gdb.COMMAND_USER, gdb.COMPLETE_NONE)

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Execute the svd command.

        Parameters
        ----------
        arg : str
            The argument string supplied by the user.
        from_tty : bool
            Whether GDB invoked the command interactively from its terminal.

        Raises
        ------
        gdb.GdbError
            If arguments or operations fail.
        """
        del from_tty
        tokens = shlex.split(arg) if arg.strip() else []
        if not tokens or tokens[0].lower() in ("help", "-h", "--help"):
            render_help()
            return

        subcmd = tokens[0].lower()
        subargs = tokens[1:]

        if subcmd == "load":
            self._invoke_load(subargs)
        elif subcmd == "read":
            self._invoke_read(subargs)
        elif subcmd == "show":
            self._invoke_show(subargs)
        elif subcmd == "write":
            self._invoke_write(subargs)
        elif subcmd == "monitor":
            self._invoke_monitor(subargs)
        elif subcmd == "dump":
            self._invoke_dump(subargs)
        else:
            raise gdb.GdbError(f"Unknown svd subcommand '{subcmd}'. Run 'svd help' for usage.")

    def complete(self, text: str, word: str) -> list[str]:
        """Provide intelligent GDB auto-completion for SVD subcommands, files, devices, and registers.

        Parameters
        ----------
        text : str
            The full argument text typed so far.
        word : str
            The specific word currently being completed.

        Returns
        -------
        list[str]
            Matching completion candidates.
        """
        subcommands = ["load", "read", "show", "write", "monitor", "dump", "help"]
        tokens = text.split()

        # Completing the first word / subcommand
        if not tokens or (len(tokens) == 1 and not text.endswith(" ")):
            return [c for c in subcommands if c.startswith(word.lower())]

        subcmd = tokens[0].lower()

        # Auto-completion for file path on 'svd read <file.svd>'
        if subcmd == "read":
            if (len(tokens) == 1 and text.endswith(" ")) or (
                len(tokens) == 2 and not text.endswith(" ")
            ):
                pattern = f"{word}*"
                expanded = os.path.expanduser(pattern)
                matches = glob.glob(expanded)
                results: list[str] = []
                for m in matches:
                    if os.path.isdir(m):
                        results.append(f"{m}/")
                    elif m.lower().endswith((".svd", ".xml")):
                        results.append(m)
                return results
            return []

        # Auto-completion for subcommands that target peripherals and registers
        if subcmd in ("show", "write", "monitor", "dump"):
            if SESSION.device is None:
                return []

            # Completing device / peripheral name
            if (len(tokens) == 1 and text.endswith(" ")) or (
                len(tokens) == 2 and not text.endswith(" ")
            ):
                periph_names = [p.name for p in SESSION.device.peripherals]
                if subcmd == "dump":
                    periph_names.append("all")
                    periph_names.append(SESSION.device.name)
                return [p for p in periph_names if p.upper().startswith(word.upper())]

            # Completing register name
            if subcmd in ("show", "write", "monitor"):
                if (len(tokens) == 2 and text.endswith(" ")) or (
                    len(tokens) == 3 and not text.endswith(" ")
                ):
                    periph_name = tokens[1]
                    periph = SESSION.device.get_peripheral(periph_name)
                    if periph is not None:
                        reg_names = [r.name for r in periph.registers]
                        return [r for r in reg_names if r.upper().startswith(word.upper())]

        return []

    def _invoke_load(self, args: list[str]) -> None:
        """Handle 'svd load' subcommand.

        Parameters
        ----------
        args : list[str]
            Subcommand arguments (none expected).

        Raises
        ------
        gdb.GdbError
            If target memory cannot be read or detection fails.
        """
        if args:
            raise gdb.GdbError("'svd load' takes no arguments.")

        try:
            reader = TargetMemoryReader()
            raw_cpuid = reader.read_uint32(CPUID_ADDRESS)
            cpuid = decode_cpuid(raw_cpuid)
            report = DEFAULT_PROVIDER_REGISTRY.inspect(reader, cpuid)
        except TargetReadError as err:
            raise gdb.GdbError(f"Cannot read target memory: {err}") from err

        try:
            svd_path, candidate_name = resolve_and_fetch_svd(report)
            device = parse_svd_file(svd_path)
            SESSION.device = device
            SESSION.svd_dict = device.to_dict()
            SESSION.svd_path = svd_path

            prod_line = report.product_line.value if report.product_line.is_available else None
            render_load_success(
                device=device,
                svd_filename=candidate_name,
                cache_path=svd_path,
                vendor=report.vendor,
                product_line=prod_line,
            )
        except SvdNotFoundError as err:
            prod_info = (
                f"{report.vendor} {report.product_line.value}"
                if report.product_line.is_available
                else f"vendor {report.vendor}"
            )
            CONSOLE.print(
                Panel(
                    Text.assemble(
                        ("SVD Not Found: ", "bold yellow"),
                        (f"No matching SVD file found for {prod_info}.\n", "default"),
                        ("Details: ", "bold"),
                        (f"{err}\n", "dim"),
                        ("Tip: ", "bold cyan"),
                        (
                            "You can supply an SVD file explicitly via 'svd read <file.svd>'.",
                            "default",
                        ),
                    ),
                    title="SVD Resolution Notice",
                    box=box.ROUNDED,
                )
            )
        except SvdError as err:
            raise gdb.GdbError(f"SVD operation failed: {err}") from err

    def _invoke_read(self, args: list[str]) -> None:
        """Handle 'svd read <file.svd>' subcommand.

        Parameters
        ----------
        args : list[str]
            Subcommand arguments (expecting 1 SVD file path).

        Raises
        ------
        gdb.GdbError
            If arguments are invalid or file parsing fails.
        """
        if len(args) != 1:
            raise gdb.GdbError("Usage: svd read <file.svd>")

        file_path = Path(args[0]).expanduser().resolve()
        if not file_path.is_file():
            raise gdb.GdbError(f"SVD file not found: {file_path}")

        try:
            device = parse_svd_file(file_path)
            SESSION.device = device
            SESSION.svd_dict = device.to_dict()
            SESSION.svd_path = file_path
            render_file_load_success(device, file_path)
        except SvdError as err:
            raise gdb.GdbError(f"Failed to parse SVD file: {err}") from err

    def _invoke_show(self, args: list[str]) -> None:
        """Handle 'svd show <device_name> [<register_name>]' subcommand.

        Parameters
        ----------
        args : list[str]
            Subcommand arguments: <device_name> [<register_name>].

        Raises
        ------
        gdb.GdbError
            If arguments are invalid or target reading fails.
        """
        if not args:
            raise gdb.GdbError("Usage: svd show <device_name> [<register_name>]")

        if SESSION.device is None:
            raise gdb.GdbError(
                "No SVD definition currently loaded. Run 'svd load' or 'svd read <file.svd>' first."
            )

        dev_name = args[0]
        periph = SESSION.device.get_peripheral(dev_name)
        if periph is None:
            raise gdb.GdbError(
                f"Peripheral '{dev_name}' not found in loaded SVD device '{SESSION.device.name}'."
            )

        try:
            reader = TargetMemoryReader()
        except TargetReadError as err:
            raise gdb.GdbError(f"Cannot access target memory: {err}") from err

        if len(args) == 1:
            render_peripheral_state(periph, reader)
        elif len(args) == 2:
            reg_name = args[1]
            reg = periph.get_register(reg_name)
            if reg is None:
                raise gdb.GdbError(
                    f"Register '{reg_name}' not found in peripheral '{periph.name}'."
                )
            try:
                val = _read_register_value(reader, periph.base_address, reg)
            except TargetReadError as err:
                raise gdb.GdbError(
                    f"Failed to read register {periph.name}->{reg.name}: {err}"
                ) from err
            render_register_detail(periph, reg, val)
        else:
            raise gdb.GdbError(
                "Too many arguments. Usage: svd show <device_name> [<register_name>]"
            )

    def _invoke_write(self, args: list[str]) -> None:
        """Handle 'svd write <device_name> <register_name> <value_hex>' subcommand.

        Parameters
        ----------
        args : list[str]
            Subcommand arguments.

        Raises
        ------
        gdb.GdbError
            If arguments are invalid or writing fails.
        """
        if len(args) != 3:
            raise gdb.GdbError("Usage: svd write <device_name> <register_name> <value_hex>")

        if SESSION.device is None:
            raise gdb.GdbError(
                "No SVD definition currently loaded. Run 'svd load' or 'svd read <file.svd>' first."
            )

        dev_name, reg_name, val_str = args[0], args[1], args[2]
        periph = SESSION.device.get_peripheral(dev_name)
        if periph is None:
            raise gdb.GdbError(
                f"Peripheral '{dev_name}' not found in loaded SVD device '{SESSION.device.name}'."
            )

        reg = periph.get_register(reg_name)
        if reg is None:
            raise gdb.GdbError(f"Register '{reg_name}' not found in peripheral '{periph.name}'.")

        try:
            value = _parse_numeric_value(val_str)
        except ValueError as err:
            raise gdb.GdbError(
                f"Invalid numeric value '{val_str}': must be hex (0x...), bin (0b...), or decimal."
            ) from err

        max_val = (1 << reg.size) - 1
        if value < 0 or value > max_val:
            raise gdb.GdbError(
                f"Value 0x{value:X} exceeds register width ({reg.size}-bit, max: 0x{max_val:X})."
            )

        try:
            reader = TargetMemoryReader()
            old_val = _read_register_value(reader, periph.base_address, reg)
        except TargetReadError:
            old_val = None

        try:
            _write_register_value(periph.base_address, reg, value)
        except Exception as err:
            raise gdb.GdbError(
                f"Failed to write to register {periph.name}->{reg.name}: {err}"
            ) from err

        try:
            readback = _read_register_value(reader, periph.base_address, reg)
        except TargetReadError:
            readback = None

        hex_len = max(2, (reg.size + 3) // 4)
        reg_addr = periph.base_address + reg.address_offset

        table = Table(
            title=f"Write Register: {periph.name}->{reg.name}",
            box=box.SIMPLE_HEAVY,
            header_style="bold cyan",
            show_header=True,
        )
        table.add_column("Property", style="bold", no_wrap=True)
        table.add_column("Value")
        table.add_row("Address", f"0x{reg_addr:08X}")
        if old_val is not None:
            table.add_row("Previous Value", f"0x{old_val:0{hex_len}X}")
        table.add_row("Written Value", Text(f"0x{value:0{hex_len}X}", style="bold green"))
        if readback is not None:
            table.add_row("Readback Value", f"0x{readback:0{hex_len}X}")
        CONSOLE.print(table)

    def _invoke_monitor(self, args: list[str]) -> None:
        """Handle 'svd monitor <device_name> <regname>' subcommand.

        Parameters
        ----------
        args : list[str]
            Subcommand arguments.

        Raises
        ------
        gdb.GdbError
            If arguments are invalid or watchpoint creation fails.
        """
        if len(args) != 2:
            raise gdb.GdbError("Usage: svd monitor <device_name> <regname>")

        if SESSION.device is None:
            raise gdb.GdbError(
                "No SVD definition currently loaded. Run 'svd load' or 'svd read <file.svd>' first."
            )

        dev_name, reg_name = args[0], args[1]
        periph = SESSION.device.get_peripheral(dev_name)
        if periph is None:
            raise gdb.GdbError(
                f"Peripheral '{dev_name}' not found in loaded SVD device '{SESSION.device.name}'."
            )

        reg = periph.get_register(reg_name)
        if reg is None:
            raise gdb.GdbError(f"Register '{reg_name}' not found in peripheral '{periph.name}'.")

        reg_addr = periph.base_address + reg.address_offset
        try:
            reader = TargetMemoryReader()
            initial_val = _read_register_value(reader, periph.base_address, reg)
        except TargetReadError:
            initial_val = 0

        c_type = _get_c_type_for_size(reg.size)
        spec = f"*({c_type} *){reg_addr:#x}"

        try:
            wp = SvdWatchpoint(
                spec=spec,
                peripheral_name=periph.name,
                register_name=reg.name,
                register=reg,
                address=reg_addr,
                initial_value=initial_val,
            )
            SESSION.watchpoints.append(wp)
        except gdb.error as err:
            raise gdb.GdbError(f"Failed to create watchpoint on {spec}: {err}") from err

        hex_len = max(2, (reg.size + 3) // 4)
        table = Table(
            title="SVD Register Monitor Activated",
            box=box.SIMPLE_HEAVY,
            header_style="bold cyan",
            show_header=True,
        )
        table.add_column("Property", style="bold", no_wrap=True)
        table.add_column("Value")
        table.add_row("Peripheral", periph.name)
        table.add_row("Register", reg.name)
        table.add_row("Address", f"0x{reg_addr:08X}")
        table.add_row("Initial Value", f"0x{initial_val:0{hex_len}X}")
        table.add_row("Watch Expression", spec)
        table.add_row("Status", Text("Active (will notify on modification)", style="bold green"))
        CONSOLE.print(table)

    def _invoke_dump(self, args: list[str]) -> None:
        """Handle 'svd dump <device_name> file.json' subcommand.

        Parameters
        ----------
        args : list[str]
            Subcommand arguments.

        Raises
        ------
        gdb.GdbError
            If arguments are invalid or dumping fails.
        """
        if len(args) != 2:
            raise gdb.GdbError("Usage: svd dump <device_name> file.json")

        if SESSION.device is None:
            raise gdb.GdbError(
                "No SVD definition currently loaded. Run 'svd load' or 'svd read <file.svd>' first."
            )

        dev_name, out_file_str = args[0], args[1]
        out_path = Path(out_file_str).expanduser().resolve()

        try:
            reader = TargetMemoryReader()
        except TargetReadError as err:
            raise gdb.GdbError(f"Cannot access target memory: {err}") from err

        dump_data: dict[str, Any] = {
            "device": SESSION.device.name,
            "vendor": SESSION.device.vendor,
            "version": SESSION.device.version,
            "timestamp_gdb": True,
            "peripherals": {},
        }

        # Check if user requested all peripherals or a specific peripheral
        if dev_name.lower() in ("all", SESSION.device.name.lower()):
            target_peripherals = list(SESSION.device.peripherals)
        else:
            periph = SESSION.device.get_peripheral(dev_name)
            if periph is None:
                raise gdb.GdbError(
                    f"Peripheral '{dev_name}' not found in loaded SVD device '{SESSION.device.name}'."
                )
            target_peripherals = [periph]

        total_regs = 0
        for p in target_peripherals:
            p_dict: dict[str, Any] = {
                "name": p.name,
                "description": p.description,
                "base_address": f"0x{p.base_address:08X}",
                "base_address_int": p.base_address,
                "group_name": p.group_name,
                "registers": {},
            }
            for r in p.registers:
                total_regs += 1
                reg_addr = p.base_address + r.address_offset
                hex_len = max(2, (r.size + 3) // 4)
                try:
                    val = _read_register_value(reader, p.base_address, r)
                    val_hex = f"0x{val:0{hex_len}X}"
                    decoded_fields = r.decode(val)
                    read_error = None
                except TargetReadError as read_err:
                    val = None
                    val_hex = None
                    decoded_fields = []
                    read_error = str(read_err)

                r_dict: dict[str, Any] = {
                    "name": r.name,
                    "display_name": r.display_name,
                    "description": r.description,
                    "address_offset": f"0x{r.address_offset:04X}",
                    "address": f"0x{reg_addr:08X}",
                    "size": r.size,
                    "access": r.access,
                    "reset_value": (
                        f"0x{r.reset_value:0{hex_len}X}" if r.reset_value is not None else None
                    ),
                    "value_raw": val,
                    "value_hex": val_hex,
                    "fields": decoded_fields,
                }
                if read_error:
                    r_dict["error"] = read_error
                p_dict["registers"][r.name] = r_dict

            dump_data["peripherals"][p.name] = p_dict

        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", encoding="utf-8") as json_file:
                json.dump(dump_data, json_file, indent=2)
        except OSError as err:
            raise gdb.GdbError(f"Failed to write dump to '{out_path}': {err}") from err

        file_size = out_path.stat().st_size
        table = Table(
            title="SVD Memory Dump Saved",
            box=box.SIMPLE_HEAVY,
            header_style="bold cyan",
            show_header=True,
        )
        table.add_column("Property", style="bold", no_wrap=True)
        table.add_column("Value")
        table.add_row("Target", dev_name)
        table.add_row("Output File", Text(str(out_path), style="bold green"))
        table.add_row("Peripherals Dumped", str(len(target_peripherals)))
        table.add_row("Registers Dumped", str(total_regs))
        table.add_row("File Size", f"{file_size:,} bytes")
        CONSOLE.print(table)


# Instantiate the GDB command
SvdCmd()
