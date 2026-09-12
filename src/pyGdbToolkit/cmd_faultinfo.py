"""The ``fault_info`` GDB command for ARM Cortex-M fault analysis."""

from __future__ import annotations

from dataclasses import dataclass
import struct

import gdb
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .cpuid import CPUID_ADDRESS, decode_cpuid
from .models import CPUID
from .target_memory import TargetMemoryReader, TargetReadError

CONSOLE = Console(force_terminal=True)

# System Control Block (SCB) register addresses
SCB_VTOR = 0xE000ED08
SCB_SHCSR = 0xE000ED24
SCB_CFSR = 0xE000ED28
SCB_HFSR = 0xE000ED2C
SCB_DFSR = 0xE000ED30
SCB_MMFAR = 0xE000ED34
SCB_BFAR = 0xE000ED38
SCB_AFSR = 0xE000ED3C
SCB_CPACR = 0xE000ED88

# ARMv8-M Security Extension fault registers
SCB_SFSR = 0xE000EDE8
SCB_SFAR = 0xE000EDEC

EXC_RETURN_MASK = 0xFFFFFF00

EXCEPTION_NAMES: dict[int, str] = {
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

# Bit decoding dictionaries: bit -> (flag_name, description)
UFSR_BITS: dict[int, tuple[str, str]] = {
    0: ("UNDEFINSTR", "Undefined instruction executed"),
    1: ("INVSTATE", "Invalid Thumb state (bit T=0 or branch to even address)"),
    2: ("INVPC", "Invalid EXC_RETURN value on exception return"),
    3: ("NOCP", "Access to disabled or non-existent coprocessor/FPU"),
    4: ("STKOF", "Stack overflow detected"),
    8: ("UNALIGNED", "Unaligned access trapped (CCR.UNALIGN_TRP=1)"),
    9: ("DIVBYZERO", "Integer division by zero (CCR.DIV_0_TRP=1)"),
}

BFSR_BITS: dict[int, tuple[str, str]] = {
    0: ("IBUSERR", "Bus error on instruction fetch"),
    1: ("PRECISERR", "Precise bus error on data access (BFAR valid)"),
    2: ("IMPRECISERR", "Imprecise bus error (asynchronous write buffer)"),
    3: ("UNSTKERR", "Bus error on exception return unstacking"),
    4: ("STKERR", "Bus error on exception entry stacking"),
    5: ("LSPERR", "Bus error during FPU lazy preservation"),
    7: ("BFARVALID", "BFAR contains valid fault address"),
}

MMFSR_BITS: dict[int, tuple[str, str]] = {
    0: ("IACCVIOL", "MPU/security violation on instruction fetch (XN)"),
    1: ("DACCVIOL", "MPU/security violation on data access"),
    3: ("MUNSTKERR", "MPU violation on exception return unstacking"),
    4: ("MSTKERR", "MPU violation on exception entry stacking"),
    5: ("MLSPERR", "MPU violation during FPU lazy preservation"),
    7: ("MMARVALID", "MMFAR contains valid fault address"),
}

HFSR_BITS: dict[int, tuple[str, str]] = {
    1: ("VECTTBL", "Vector table read error"),
    30: ("FORCED", "Fault escalated to HardFault (source handler disabled/masked)"),
    31: ("DEBUGEVT", "Debug event / breakpoint"),
}

DFSR_BITS: dict[int, tuple[str, str]] = {
    0: ("HALTED", "Core halted by debug request / DAP"),
    1: ("BKPT", "BKPT instruction executed"),
    2: ("DWTTRAP", "DWT trap / watchpoint"),
    3: ("VCATCH", "Vector catch triggered"),
    4: ("EXTERNAL", "External debug request"),
}

SHCSR_BITS: dict[int, tuple[str, str]] = {
    0: ("MEMFAULTACT", "MemManage active"),
    1: ("BUSFAULTACT", "BusFault active"),
    3: ("USGFAULTACT", "UsageFault active"),
    7: ("SVCALLACT", "SVCall active"),
    8: ("MONITORACT", "Debug Monitor active"),
    10: ("PENDSVACT", "PendSV active"),
    11: ("SYSTICKACT", "SysTick active"),
    12: ("USGFAULTPENDED", "UsageFault pending"),
    13: ("MEMFAULTPENDED", "MemManage pending"),
    14: ("BUSFAULTPENDED", "BusFault pending"),
    15: ("SVCALLPENDED", "SVCall pending"),
    16: ("MEMFAULTENA", "MemManage enabled"),
    17: ("BUSFAULTENA", "BusFault enabled"),
    18: ("USGFAULTENA", "UsageFault enabled"),
    19: ("SECUREFAULTENA", "SecureFault enabled"),
    20: ("SECUREFAULTPENDED", "SecureFault pending"),
    21: ("HARDFAULTPENDED", "HardFault pending"),
}

SFSR_BITS: dict[int, tuple[str, str]] = {
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
    fp_registers: list[int] | None = None
    fpscr: int | None = None
    s0_float: float | None = None


def _symbolicate(address: int | None) -> str:
    """Resolve a target memory address to a symbol location using GDB.

    Parameters
    ----------
    address : int | None
        The target address to resolve.

    Returns
    -------
    str
        The symbol name and offset, or ``"?"`` if unavailable.
    """
    if address is None:
        return "?"
    try:
        output = gdb.execute(f"info symbol 0x{address:X}", to_string=True).strip()
        if "No symbol matches" in output or "not in valid memory" in output:
            return "?"
        return output.split(" in section ")[0]
    except Exception:
        return "?"


def _memory_region(address: int | None) -> str:
    """Return the architected ARM Cortex-M memory region name for an address.

    Parameters
    ----------
    address
        Target address.

    Returns
    -------
    str
        Descriptive name of the memory zone.
    """
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


def _decode_flags(value: int | None, table: dict[int, tuple[str, str]]) -> list[tuple[str, str]]:
    """Decode bitfield flags from a numeric register value.

    Parameters
    ----------
    value
        Register raw integer value.
    table
        Dictionary mapping bit position to (flag_name, description).

    Returns
    -------
    list of tuple of str
        List of (flag_name, description) for all active bits.
    """
    if value is None:
        return []
    flags: list[tuple[str, str]] = []
    for bit, (name, desc) in sorted(table.items()):
        if value & (1 << bit):
            flags.append((name, desc))
    return flags


def _read_stacked_frame(
    reader: TargetMemoryReader,
    lr_val: int,
    sp_val: int,
    sp_name: str,
) -> StackedFrame | str:
    """Read and decode the hardware-stacked exception frame from target memory.

    Parameters
    ----------
    reader
        Memory reader instance.
    lr_val
        EXC_RETURN value currently in LR.
    sp_val
        Stack pointer address at fault entry.
    sp_name
        Stack name (``"MSP"`` or ``"PSP"``).

    Returns
    -------
    StackedFrame or str
        The decoded exception frame, or an error string if reading failed.
    """
    is_secure = ((lr_val >> 0) & 1) == 0
    target_domain = "Secure" if is_secure else "Non-Secure"
    return_mode = "Handler" if ((lr_val >> 3) & 1) == 0 else "Thread"
    has_fpu = ((lr_val >> 4) & 1) == 0
    secure_stacking = bool((lr_val >> 6) & 1)

    words_count = 8 + (18 if has_fpu else 0)
    words: list[int] = []
    for i in range(words_count):
        try:
            words.append(reader.read_uint32(sp_val + 4 * i))
        except (TargetReadError, ValueError) as error:
            return f"Cannot read stacked frame at 0x{sp_val + 4 * i:08X}: {error}"

    fp_regs: list[int] | None = None
    fpscr: int | None = None
    s0_flt: float | None = None
    if has_fpu:
        fp_regs = words[8:24]
        fpscr = words[24]
        try:
            s0_flt = struct.unpack("<f", struct.pack("<I", fp_regs[0]))[0]
        except Exception:
            s0_flt = None

    return StackedFrame(
        lr_exc_return=lr_val,
        sp_name=sp_name,
        sp_address=sp_val,
        return_mode=return_mode,
        target_domain=target_domain,
        has_fpu=has_fpu,
        secure_stacking=secure_stacking,
        r0=words[0],
        r1=words[1],
        r2=words[2],
        r3=words[3],
        r12=words[4],
        lr=words[5],
        pc=words[6],
        xpsr=words[7],
        fp_registers=fp_regs,
        fpscr=fpscr,
        s0_float=s0_flt,
    )


class FaultInfoCmd(gdb.Command):
    """Analyze and display ARM Cortex-M fault status registers and stacked exception frames."""

    def __init__(self) -> None:
        """Register the command with GDB."""
        super().__init__("fault_info", gdb.COMMAND_USER)

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Execute the fault_info command.

        Parameters
        ----------
        arg
            Command arguments (none expected).
        from_tty
            Whether GDB invoked the command from its terminal.

        Raises
        ------
        gdb.GdbError
            If arguments are supplied or target is unsupported.
        """
        del from_tty
        if arg.strip():
            raise gdb.GdbError("fault_info does not accept arguments")

        frame = gdb.selected_frame()
        architecture_name = frame.architecture().name().lower()
        if not any(k in architecture_name for k in ("arm", "cortex-m", "thumb")):
            raise gdb.GdbError("fault_info only supports ARM Cortex-M targets")

        def read_optional(name: str) -> int | None:
            try:
                return int(frame.read_register(name))
            except (gdb.error, ValueError, TypeError):
                return None

        try:
            reader = TargetMemoryReader()
        except TargetReadError as error:
            raise gdb.GdbError(str(error)) from error

        cpuid: CPUID | None = None
        try:
            cpuid = decode_cpuid(reader.read_uint32(CPUID_ADDRESS))
        except (TargetReadError, ValueError):
            cpuid = None

        pc = int(frame.read_register("pc"))
        lr = read_optional("lr")
        xpsr = read_optional("xpsr")
        ipsr_value = read_optional("ipsr")
        ipsr = (
            ipsr_value & 0x1FF
            if ipsr_value is not None
            else (xpsr & 0x1FF if xpsr is not None else 0)
        )

        # Read SCB registers
        scb_regs = self._read_scb_registers(reader)

        # Decode stacked frame if LR contains an EXC_RETURN value
        stacked_frame: StackedFrame | str | None = None
        if lr is not None and (lr & EXC_RETURN_MASK) == EXC_RETURN_MASK:
            spsel = bool((lr >> 2) & 1)
            sp_name = "PSP" if spsel else "MSP"
            sp_val = read_optional("psp" if spsel else "msp")
            if sp_val is None:
                sp_val = int(frame.read_register("sp"))
            stacked_frame = _read_stacked_frame(reader, lr, sp_val, sp_name)

        self._render_report(
            cpuid=cpuid,
            ipsr=ipsr,
            pc=pc,
            lr=lr,
            xpsr=xpsr,
            scb=scb_regs,
            stacked_frame=stacked_frame,
        )

    def _read_scb_registers(self, reader: TargetMemoryReader) -> dict[str, int | None]:
        """Read SCB fault and configuration registers.

        Parameters
        ----------
        reader
            Target memory reader.

        Returns
        -------
        dict of str to int or None
            Mapping of register names to their read values.
        """
        regs: dict[str, int | None] = {}
        addresses = {
            "SHCSR": SCB_SHCSR,
            "CFSR": SCB_CFSR,
            "HFSR": SCB_HFSR,
            "DFSR": SCB_DFSR,
            "MMFAR": SCB_MMFAR,
            "BFAR": SCB_BFAR,
            "AFSR": SCB_AFSR,
            "VTOR": SCB_VTOR,
            "CPACR": SCB_CPACR,
            "SFSR": SCB_SFSR,
            "SFAR": SCB_SFAR,
        }
        for name, addr in addresses.items():
            try:
                regs[name] = reader.read_uint32(addr)
            except (TargetReadError, ValueError):
                regs[name] = None
        return regs

    def _render_report(
        self,
        cpuid: CPUID | None,
        ipsr: int,
        pc: int | None,
        lr: int | None,
        xpsr: int | None,
        scb: dict[str, int | None],
        stacked_frame: StackedFrame | str | None,
    ) -> None:
        """Render the complete Rich fault report to the console.

        Parameters
        ----------
        cpuid
            Decoded CPUID if available.
        ipsr
            Active exception number from IPSR.
        pc
            Current Program Counter.
        lr
            Current Link Register.
        xpsr
            Current Program Status Register.
        scb
            Dictionary of read SCB registers.
        stacked_frame
            Decoded stacked frame or error message.
        """
        # 1. Overview Table
        self._render_overview(cpuid, ipsr, pc, lr, xpsr)

        # 2. Stacked Exception Frame Table
        if isinstance(stacked_frame, StackedFrame):
            self._render_stacked_frame_table(stacked_frame)
        elif isinstance(stacked_frame, str):
            CONSOLE.print(
                Panel(
                    Text(stacked_frame, style="bold red"),
                    title="Stack Frame",
                    box=box.SIMPLE_HEAVY,
                )
            )

        # 3. SCB Fault Status Registers Table
        self._render_scb_table(scb)

        # 4. Fault Diagnostics & Probable Causes
        self._render_diagnostics(scb, stacked_frame)

    def _render_overview(
        self,
        cpuid: CPUID | None,
        ipsr: int,
        pc: int | None,
        lr: int | None,
        xpsr: int | None,
    ) -> None:
        """Render the overview table of current execution and exception state.

        Parameters
        ----------
        cpuid
            Decoded CPUID if available.
        ipsr
            Active exception number from IPSR.
        pc
            Current Program Counter.
        lr
            Current Link Register.
        xpsr
            Current Program Status Register.
        """
        table = Table(
            title="ARM Cortex-M Fault Overview",
            box=box.SIMPLE_HEAVY,
            header_style="bold cyan",
            show_header=True,
        )
        table.add_column("Property", style="bold", no_wrap=True)
        table.add_column("Value")

        if cpuid is not None:
            table.add_row("Target Core", f"{cpuid.core} ({cpuid.rnp_revision}) - {cpuid.implementer_name}")
        else:
            table.add_row("Target Core", "Generic Cortex-M")

        exc_name = EXCEPTION_NAMES.get(ipsr, f"Interrupt #{ipsr}")
        if ipsr in (2, 3, 4, 5, 6, 7):
            exc_display = Text.assemble((f"{exc_name} (Exception #{ipsr})", "bold red"))
        elif ipsr == 0:
            exc_display = Text("None (Thread Mode)", style="green")
        else:
            exc_display = Text(f"{exc_name} (#{ipsr})")
        table.add_row("Active Exception", exc_display)

        mode_str = "HANDLER (in exception)" if ipsr else "THREAD (normal execution)"
        table.add_row("Execution Mode", mode_str)

        pc_str = f"0x{pc:08X} [{_symbolicate(pc)}]" if pc is not None else "Unknown"
        table.add_row("Current PC", pc_str)

        if lr is not None:
            is_exc_ret = (lr & EXC_RETURN_MASK) == EXC_RETURN_MASK
            lr_suffix = " (valid EXC_RETURN)" if is_exc_ret else f" [{_symbolicate(lr)}]"
            table.add_row("Current LR", f"0x{lr:08X}{lr_suffix}")
        else:
            table.add_row("Current LR", "Unknown")

        if xpsr is not None:
            t_bit = (xpsr >> 24) & 1
            table.add_row("Current xPSR", f"0x{xpsr:08X} (IPSR={xpsr & 0x1FF}, T={t_bit})")

        CONSOLE.print(table)

    def _render_stacked_frame_table(self, frame: StackedFrame) -> None:
        """Render the decoded hardware-stacked exception registers.

        Parameters
        ----------
        frame
            The decoded hardware stacked frame.
        """
        summary_text = (
            f"Stack: [bold cyan]{frame.sp_name}[/bold cyan] @ 0x{frame.sp_address:08X} | "
            f"Return to: [bold]{frame.return_mode}[/bold] ({frame.target_domain}) | "
            f"Frame: [bold]{'Extended (standard FPU)' if frame.has_fpu else 'Basic (8 registers)'}[/bold]"
        )

        table = Table(
            title=f"Stacked frame at crash time ({summary_text})",
            box=box.SIMPLE_HEAVY,
            header_style="bold cyan",
            show_header=True,
        )
        table.add_column("Register", style="bold", no_wrap=True)
        table.add_column("Stacked Value", style="bold")
        table.add_column("Details / Symbol")

        table.add_row("r0", f"0x{frame.r0:08X}", "")
        table.add_row("r1", f"0x{frame.r1:08X}", "")
        table.add_row("r2", f"0x{frame.r2:08X}", "")
        table.add_row("r3", f"0x{frame.r3:08X}", "")
        table.add_row("r12", f"0x{frame.r12:08X}", "")
        table.add_row(
            "lr",
            f"0x{frame.lr:08X}",
            f"[dim]Caller:[/dim] {_symbolicate(frame.lr)}",
        )
        table.add_row(
            "pc",
            f"[bold red]0x{frame.pc:08X}[/bold red]",
            f"[bold red]<-- Faulting instruction:[/bold red] {_symbolicate(frame.pc)}",
        )

        t_bit = (frame.xpsr >> 24) & 1
        ipsr_val = frame.xpsr & 0x1FF
        table.add_row(
            "xpsr",
            f"0x{frame.xpsr:08X}",
            f"IPSR={ipsr_val}, T={t_bit} ({'Thumb' if t_bit else '[bold red]ARM (invalid on Cortex-M)[/bold red]'})",
        )

        if frame.has_fpu and frame.fp_registers is not None:
            for i, val in enumerate(frame.fp_registers):
                detail = f"s0 (float32) = {frame.s0_float:.7g}" if i == 0 and frame.s0_float is not None else ""
                table.add_row(f"s{i}", f"0x{val:08X}", detail)
            if frame.fpscr is not None:
                table.add_row("fpscr", f"0x{frame.fpscr:08X}", "FPU Status & Control")

        CONSOLE.print(table)

    def _render_scb_table(self, scb: dict[str, int | None]) -> None:
        """Render the SCB fault status registers table.

        Parameters
        ----------
        scb
            Dictionary of read SCB registers.
        """
        table = Table(
            title="SCB (System Control Block) Status Registers",
            box=box.SIMPLE_HEAVY,
            header_style="bold cyan",
            show_header=True,
        )
        table.add_column("Register", style="bold", no_wrap=True)
        table.add_column("Value", style="bold", no_wrap=True)
        table.add_column("Active Flags & Meaning")

        # CFSR
        cfsr = scb.get("CFSR")
        if cfsr is not None:
            mmfsr = cfsr & 0xFF
            bfsr = (cfsr >> 8) & 0xFF
            ufsr = (cfsr >> 16) & 0xFFFF

            cfsr_flags = []
            for name, desc in _decode_flags(mmfsr, MMFSR_BITS):
                cfsr_flags.append(f"[bold red]{name}[/bold red]: {desc} (MemManage)")
            for name, desc in _decode_flags(bfsr, BFSR_BITS):
                cfsr_flags.append(f"[bold red]{name}[/bold red]: {desc} (BusFault)")
            for name, desc in _decode_flags(ufsr, UFSR_BITS):
                cfsr_flags.append(f"[bold red]{name}[/bold red]: {desc} (UsageFault)")

            desc_text = "\n".join(cfsr_flags) if cfsr_flags else "[green]No active error flags[/green]"
            table.add_row("CFSR", f"0x{cfsr:08X}", desc_text)
            table.add_row(" ├─ MMFSR", f"0x{mmfsr:02X}", f"{len(_decode_flags(mmfsr, MMFSR_BITS))} active flag(s)")
            table.add_row(" ├─ BFSR", f"0x{bfsr:02X}", f"{len(_decode_flags(bfsr, BFSR_BITS))} active flag(s)")
            table.add_row(" └─ UFSR", f"0x{ufsr:04X}", f"{len(_decode_flags(ufsr, UFSR_BITS))} active flag(s)")
        else:
            table.add_row("CFSR", "[yellow]Unavailable[/yellow]", "Not present on this target or memory inaccessible")

        # HFSR
        hfsr = scb.get("HFSR")
        if hfsr is not None:
            hfsr_flags = [f"[bold yellow]{name}[/bold yellow]: {desc}" for name, desc in _decode_flags(hfsr, HFSR_BITS)]
            desc_text = "\n".join(hfsr_flags) if hfsr_flags else "[green]No active error flags[/green]"
            table.add_row("HFSR", f"0x{hfsr:08X}", desc_text)

        # MMFAR & BFAR
        mmfar = scb.get("MMFAR")
        mmarvalid = bool(cfsr and (cfsr & 0x80))
        if mmfar is not None:
            valid_tag = "[bold green][VALID][/bold green]" if mmarvalid else "[dim][INVALID][/dim]"
            table.add_row("MMFAR", f"0x{mmfar:08X}", f"{valid_tag} Region: {_memory_region(mmfar)}")

        bfar = scb.get("BFAR")
        bfarvalid = bool(cfsr and ((cfsr >> 8) & 0x80))
        if bfar is not None:
            valid_tag = "[bold green][VALID][/bold green]" if bfarvalid else "[dim][INVALID][/dim]"
            table.add_row("BFAR", f"0x{bfar:08X}", f"{valid_tag} Region: {_memory_region(bfar)}")

        # DFSR
        dfsr = scb.get("DFSR")
        if dfsr is not None:
            dfsr_flags = [f"[cyan]{name}[/cyan]: {desc}" for name, desc in _decode_flags(dfsr, DFSR_BITS)]
            desc_text = "\n".join(dfsr_flags) if dfsr_flags else "[dim]No debug event[/dim]"
            table.add_row("DFSR", f"0x{dfsr:08X}", desc_text)

        # SHCSR
        shcsr = scb.get("SHCSR")
        if shcsr is not None:
            enabled = []
            if shcsr & (1 << 16):
                enabled.append("[green]MemManage[/green]")
            if shcsr & (1 << 17):
                enabled.append("[green]BusFault[/green]")
            if shcsr & (1 << 18):
                enabled.append("[green]UsageFault[/green]")
            if shcsr & (1 << 19):
                enabled.append("[green]SecureFault[/green]")
            ena_text = ", ".join(enabled) if enabled else "[bold red]None (direct escalation to HardFault)[/bold red]"
            table.add_row("SHCSR", f"0x{shcsr:08X}", f"Enabled configurable handlers: {ena_text}")

        # VTOR
        vtor = scb.get("VTOR")
        if vtor is not None:
            table.add_row("VTOR", f"0x{vtor:08X}", f"Vector table @ {_memory_region(vtor)}")

        # SFSR / SFAR if Security Extension (ARMv8-M)
        sfsr = scb.get("SFSR")
        if sfsr is not None and sfsr not in (0, 0xFFFFFFFF):
            sfsr_flags = [f"[bold red]{name}[/bold red]: {desc}" for name, desc in _decode_flags(sfsr, SFSR_BITS)]
            table.add_row("SFSR", f"0x{sfsr:08X}", "\n".join(sfsr_flags))
            sfar = scb.get("SFAR")
            if sfar is not None and (sfsr & (1 << 6)):
                table.add_row("SFAR", f"0x{sfar:08X}", f"[bold green][VALID][/bold green] Region: {_memory_region(sfar)}")

        CONSOLE.print(table)

    def _render_diagnostics(
        self,
        scb: dict[str, int | None],
        stacked_frame: StackedFrame | str | None,
    ) -> None:
        """Analyze and display synthesis and actionable diagnostic insights.

        Parameters
        ----------
        scb
            Dictionary of read SCB registers.
        stacked_frame
            Decoded stacked frame or error message.
        """
        cfsr = scb.get("CFSR") or 0
        hfsr = scb.get("HFSR") or 0
        bfar = scb.get("BFAR")
        mmfar = scb.get("MMFAR")
        cpacr = scb.get("CPACR")

        mmfsr = cfsr & 0xFF
        bfsr = (cfsr >> 8) & 0xFF
        ufsr = (cfsr >> 16) & 0xFFFF

        items: list[str] = []

        # HardFault escalation
        if hfsr & (1 << 30):
            if bfsr:
                items.append("• [bold yellow]HardFault escalation:[/bold yellow] A BusFault was forced to HardFault (source handler disabled in SHCSR).")
            elif mmfsr:
                items.append("• [bold yellow]HardFault escalation:[/bold yellow] A MemManage Fault was forced to HardFault (source handler disabled in SHCSR).")
            elif ufsr:
                items.append("• [bold yellow]HardFault escalation:[/bold yellow] An UsageFault was forced to HardFault (source handler disabled in SHCSR).")
            else:
                items.append("• [bold yellow]HardFault escalation:[/bold yellow] Forced HardFault without configurable cause bits (handler masked by PRIMASK/FAULTMASK).")

        # BusFault analysis
        if bfsr & (1 << 1):  # PRECISERR
            if (bfsr & (1 << 7)) and bfar is not None:
                if bfar < 0x1000:
                    items.append(f"• [bold red]NULL pointer dereference:[/bold red] Invalid memory access at 0x{bfar:08X} ({_memory_region(bfar)}).")
                elif 0x40000000 <= bfar < 0x60000000:
                    items.append(f"• [bold red]Peripheral bus error (0x{bfar:08X}):[/bold red] Verify that the peripheral clock (RCC/PCLK) is enabled before any access.")
                else:
                    items.append(f"• [bold red]Precise BusFault at 0x{bfar:08X}:[/bold red] Invalid memory access in {_memory_region(bfar)} region.")
        elif bfsr & (1 << 2):  # IMPRECISERR
            items.append("• [bold red]Imprecise (asynchronous) BusFault:[/bold red] Caused by a write buffer. The stacked PC is downstream of the actual faulting access. To locate the exact access, temporarily disable write buffering (e.g. ACTLR.DISDEFWBUF).")

        if bfsr & (1 << 0):  # IBUSERR
            items.append("• [bold red]Instruction Fetch BusFault:[/bold red] Attempted execution from an invalid or inaccessible memory region (corrupted function pointer, overwritten vtable).")

        if bfsr & (1 << 4):  # STKERR
            items.append("• [bold red]Bus error during Stacking:[/bold red] The stack (MSP/PSP) overflowed or points to invalid memory.")

        if bfsr & (1 << 3):  # UNSTKERR
            items.append("• [bold red]Bus error during Unstacking:[/bold red] Corrupted stack upon exception return.")

        # MemManage analysis
        if mmfsr & (1 << 1):  # DACCVIOL
            addr_str = f" at 0x{mmfar:08X}" if (mmfsr & (1 << 7)) and mmfar is not None else ""
            items.append(f"• [bold red]MPU violation on data{addr_str}:[/bold red] Access prohibited by MPU region permissions.")
        if mmfsr & (1 << 0):  # IACCVIOL
            items.append("• [bold red]MPU violation on instruction:[/bold red] Attempted execution in an MPU region marked eXecute-Never (XN).")

        # UsageFault analysis
        if ufsr & (1 << 0):  # UNDEFINSTR
            items.append("• [bold red]Undefined instruction (UNDEFINSTR):[/bold red] Unknown or corrupted opcode (branching into data/NULL or incorrect instruction alignment).")

        if ufsr & (1 << 1):  # INVSTATE
            items.append("• [bold red]Invalid Thumb state (INVSTATE):[/bold red] Branch to an even address (bit T=0). On Cortex-M, function pointers must have the least significant bit (LSB) set to 1.")

        if ufsr & (1 << 2):  # INVPC
            items.append("• [bold red]Invalid EXC_RETURN (INVPC):[/bold red] Illegal exception return value loaded into PC (LR or stack corruption).")

        if ufsr & (1 << 3):  # NOCP
            if cpacr is not None and (cpacr & 0x00F00000) != 0x00F00000:
                items.append("• [bold red]Disabled FPU coprocessor (NOCP):[/bold red] FPU instruction executed while FPU is disabled. Add [bold cyan]SCB->CPACR |= (0xF << 20);[/bold cyan] during initialization.")
            else:
                items.append("• [bold red]Unimplemented coprocessor access (NOCP):[/bold red] Access to a non-existent or unconfigured coprocessor.")

        if ufsr & (1 << 4):  # STKOF
            items.append("• [bold red]Hardware stack overflow (STKOF):[/bold red] Stack pointer exceeded configured limit (MSPLIM/PSPLIM).")

        if ufsr & (1 << 8):  # UNALIGNED
            items.append("• [bold red]Unaligned access (UNALIGNED):[/bold red] Unaligned access trapped by CCR.UNALIGN_TRP.")

        if ufsr & (1 << 9):  # DIVBYZERO
            items.append("• [bold red]Divide by zero (DIVBYZERO):[/bold red] Integer division by zero trapped by CCR.DIV_0_TRP.")

        # Vector table fault
        if hfsr & (1 << 1):
            items.append("• [bold red]Vector Table read error (VECTTBL):[/bold red] Vector table is unreadable. Verify VTOR register configuration and Flash memory.")

        if isinstance(stacked_frame, StackedFrame):
            items.append(f"• [bold cyan]Crash location:[/bold cyan] Instruction at [bold]0x{stacked_frame.pc:08X}[/bold] ({_symbolicate(stacked_frame.pc)}), called from [bold]0x{stacked_frame.lr:08X}[/bold] ({_symbolicate(stacked_frame.lr)}).")

        if not items:
            items.append("• [green]No obvious error conditions detected in SCB registers.[/green]")

        panel_content = "\n".join(items)
        CONSOLE.print(
            Panel(
                panel_content,
                title="[bold yellow]Diagnostics & Probable Causes[/bold yellow]",
                box=box.SIMPLE_HEAVY,
            )
        )


FaultInfoCommand = FaultInfoCmd
