# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Read-only Cortex-M Zephyr stack measurements and register evidence."""

from dataclasses import dataclass
from pathlib import Path
import struct
from typing import Mapping

import gdb
from rich import box
from rich.console import Group
from rich.table import Table
from rich.text import Text

from ..target_memory import TargetMemory, TargetMemoryReader, TargetReadError

MAX_STACK_SCAN = 1024 * 1024
MAX_BACKTRACE_FRAMES = 64
_CORE_REGISTERS = ("r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr")


@dataclass(frozen=True)
class StackBounds:
    """Writable thread stack with TLS/randomization removed from the initial SP."""

    start: int
    size: int
    delta: int

    def __post_init__(self) -> None:
        """Reject corrupt or unsupported 32-bit downward-growing stack bounds."""
        if (
            self.start <= 0
            or self.start % 4
            or self.size <= 0
            or not 0 <= self.delta < self.size
            or self.start + self.size > 2**32
            or self.top % 4
        ):
            raise ValueError("Invalid Zephyr stack_info bounds or delta")

    @property
    def top(self) -> int:
        """Return the initial stack pointer, excluding reserved top storage."""
        return self.start + self.size - self.delta

    def require(self, address: int, size: int = 0) -> None:
        """Require an aligned pointer or complete byte range within usable stack."""
        if address % 4 or size < 0 or not self.start <= address <= address + size <= self.top:
            raise ValueError(f"Zephyr stack range outside bounds: 0x{address:08X} + {size}")


@dataclass(frozen=True)
class SavedContext:
    """Core register snapshot and the reconstructed pre-exception SP."""

    registers: Mapping[str, int]
    restored_sp: int
    frame_bytes: int
    extended: bool


def decode_saved_context(
    reader: TargetMemory,
    bounds: StackBounds,
    psp: int,
    callee: Mapping[str, int],
    exc_return: int,
) -> SavedContext:
    """Decode Zephyr PendSV's basic frame and account for FP and alignment storage.

    Zephyr's saved PSP points to ``__basic_sf``; an extended frame appends the
    18-word FP area after its eight core words. Only core register values are
    reported, never unverified lazy FP state.

    Parameters
    ----------
    reader : TargetMemory
        Exact, read-only target-memory reader.
    bounds : StackBounds
        Usable bounds of the matching thread's stack.
    psp : int
        Saved ``callee_saved.psp``, not the live PSP of a currently scheduled thread.
    callee : Mapping[str, int]
        R4-R11 values decoded by DWARF field names.
    exc_return : int
        Low octet saved by Zephyr, or 0xFD for a build without FP/security switching.

    Returns
    -------
    SavedContext
        Saved core registers, frame size and restored SP.

    Raises
    ------
    ValueError
        If the frame is outside bounds or its exception-return/Thumb state is invalid.
    TargetReadError
        If target memory cannot be read.
    """
    if exc_return not in (0xFD, 0xED):
        raise ValueError(f"Unsupported Zephyr saved EXC_RETURN octet: 0x{exc_return:02X}")
    extended = not bool(exc_return & 0x10)
    frame_size = 104 if extended else 32
    bounds.require(psp, frame_size)
    words = struct.unpack("<8I", reader.read_bytes(psp, 32))
    registers = dict(zip(_CORE_REGISTERS, words, strict=True))
    if not registers["xpsr"] & (1 << 24) or registers["xpsr"] & 0x1FF:
        raise ValueError("Invalid Zephyr saved xPSR: expected Thumb Thread mode")
    if registers["pc"] == 0:
        raise ValueError("Invalid Zephyr saved PC: null")
    frame_size += 4 if registers["xpsr"] & (1 << 9) else 0
    bounds.require(psp, frame_size)
    for index in range(4, 12):
        registers[f"r{index}"] = callee[f"r{index}"]
    registers["sp"] = psp + frame_size
    return SavedContext(registers, psp + frame_size, frame_size, extended)


def stack_high_water(reader: TargetMemory, bounds: StackBounds, sentinel: bool) -> tuple[int, int]:
    """Scan Zephyr's 0xAA fill within a bounded usable stack, excluding its sentinel.

    Parameters
    ----------
    reader : TargetMemory
        Read-only memory access.
    bounds : StackBounds
        Stack range with reserved top storage removed.
    sentinel : bool
        Whether the first four bytes are reserved for CONFIG_STACK_SENTINEL.

    Returns
    -------
    tuple[int, int]
        Contiguous untouched bytes and scanned capacity, excluding the sentinel.

    Raises
    ------
    ValueError
        If the stack exceeds the one-MiB inspection budget or has no scan area.
    TargetReadError
        If any requested memory is inaccessible.
    """
    start = bounds.start + (4 if sentinel else 0)
    capacity = bounds.top - start
    if not 0 < capacity <= MAX_STACK_SCAN:
        raise ValueError(f"Zephyr stack fill scan requires 1..{MAX_STACK_SCAN} bytes")
    unused = 0
    while unused < capacity:
        data = reader.read_bytes(start + unused, min(256, capacity - unused))
        count = len(data) - len(data.lstrip(b"\xaa"))
        unused += count
        if count != len(data):
            break
    return unused, capacity


def _table(title: str) -> Table:
    result = Table(title=title, box=box.SIMPLE_HEAVY)
    result.add_column("Field")
    result.add_column("Value", overflow="fold")
    return result


def _location(address: int, image: Path | None) -> str:
    if address == 0:
        return "null"
    code = address & ~1
    block = gdb.block_for_pc(code)
    while block is not None:
        function = block.function
        if function is not None:
            if image is not None and (
                function.symtab is None or function.symtab.objfile.filename != str(image)
            ):
                return "symbol unavailable in loaded project"
            return f"{function.print_name} + {code - block.start}"
        block = block.superblock
    return "no function symbol"


def show_context(
    thread: gdb.Value,
    current: bool,
    cpu_count: int,
    config: Mapping[str, str],
    image: Path | None = None,
) -> Group:
    """Display stack/register evidence without writing registers or selecting frames.

    Parameters
    ----------
    thread : gdb.Value
        Selected thread's DWARF-typed k_thread value from a halted sample.
    current : bool
        Whether this thread is referenced by a CPU's current pointer.
    cpu_count : int
        Number of CPU slots in the sampled kernel.
    config : Mapping[str, str]
        Effective Kconfig from the loaded build.
    image : Path | None
        Matching symbol image; reject function names from another loaded project.

    Returns
    -------
    Group
        Stack metrics and core-register/backtrace evidence, or explicit unavailability.

    Raises
    ------
    ValueError
        If stack bounds or a supported saved frame are corrupt, or reads fail.
    """
    unsupported = (
        "CONFIG_USE_SWITCH",
        "CONFIG_USERSPACE",
        "CONFIG_TRUSTED_EXECUTION_SECURE",
        "CONFIG_TRUSTED_EXECUTION_NONSECURE",
        "CONFIG_ARM_SECURE_FIRMWARE",
        "CONFIG_STACK_GROWS_UP",
    )
    reasons = [key for key in unsupported if config.get(key) == "y"]
    if config.get("CONFIG_CPU_CORTEX_M") != "y":
        reasons.append("not a Cortex-M configuration")
    if reasons:
        return Group(Text("Stack/context unavailable: unsupported " + ", ".join(reasons)))
    if config.get("CONFIG_THREAD_STACK_INFO") != "y":
        return Group(
            Text("Stack/context unavailable: CONFIG_THREAD_STACK_INFO is disabled or absent.")
        )
    if current and (cpu_count != 1 or config.get("CONFIG_SMP") == "y"):
        return Group(Text("Live context unavailable: cannot bind GDB registers to a Zephyr CPU."))
    try:
        stack_info = thread["stack_info"]
        bounds = StackBounds(
            int(stack_info["start"]), int(stack_info["size"]), int(stack_info["delta"])
        )
        stack = _table("Zephyr stack (end-exclusive)")
        stack.add_row("Writable range", f"0x{bounds.start:08X}-0x{bounds.start + bounds.size:08X}")
        stack.add_row("Initial SP", f"0x{bounds.top:08X}")
        stack.add_row("Reserved top (delta)", f"{bounds.delta} bytes")
        registers = _table("Core registers")
        trace = _table("Backtrace evidence")
        reader = TargetMemoryReader()
        if current:
            frame = gdb.newest_frame()
            if frame is None:
                raise ValueError("Cannot read live Zephyr context: no newest GDB frame")
            xpsr = int(frame.read_register("xpsr"))
            if xpsr & 0x1FF:
                stack.add_row(
                    "Context", "Unavailable in Handler mode; saved thread PSP may be stale."
                )
                return Group(
                    stack, Text("Stop in Thread mode for live registers and a GDB backtrace.")
                )
            if not xpsr & (1 << 24):
                raise ValueError("Invalid live Zephyr xPSR: expected Thumb state")
            values = {f"r{i}": int(frame.read_register(f"r{i}")) for i in range(13)}
            values.update({name: int(frame.read_register(name)) for name in ("sp", "lr", "pc")})
            values["xpsr"] = xpsr
            sp = values["sp"]
            bounds.require(sp)
            stack.add_row("SP source", "Live newest GDB frame (Thread mode)")
            for index in range(MAX_BACKTRACE_FRAMES):
                pc = int(frame.pc())
                trace.add_row(f"GDB #{index}", Text(f"0x{pc:08X} {frame.name() or '<unknown>'}"))
                older = frame.older()
                if older is None:
                    break
                frame = older
            else:
                trace.add_row("Limit", f"Stopped at {MAX_BACKTRACE_FRAMES} GDB frames")
        else:
            if config.get("CONFIG_FPU") == "y" and config.get("CONFIG_ARM_STORE_EXC_RETURN") != "y":
                return Group(
                    stack, Text("Saved context unavailable: FPU build lacks saved EXC_RETURN.")
                )
            if config.get("CONFIG_ARM_STORE_EXC_RETURN") != "y" and config.get("CONFIG_FPU") != "n":
                return Group(stack, Text("Saved context unavailable: CONFIG_FPU is not recorded."))
            callee = thread["callee_saved"]
            sp = int(callee["psp"])
            exc_return = (
                int(thread["arch"]["mode_exc_return"])
                if config.get("CONFIG_ARM_STORE_EXC_RETURN") == "y"
                else 0xFD
            )
            if exc_return == 0xED and (
                config.get("CONFIG_FPU") != "y" or config.get("CONFIG_FPU_SHARING") != "y"
            ):
                raise ValueError("Extended Zephyr frame contradicts FPU/FPU_SHARING configuration")
            saved = decode_saved_context(
                reader,
                bounds,
                sp,
                {f"r{i + 3}": int(callee[f"v{i}"]) for i in range(1, 9)},
                exc_return,
            )
            values = dict(saved.registers)
            stack.add_row("SP source", "Saved callee_saved.psp (exception-frame base)")
            stack.add_row("Restored SP", f"0x{saved.restored_sp:08X}")
            stack.add_row(
                "Exception frame", f"{saved.frame_bytes} bytes; extended={saved.extended}"
            )
            for name in ("pc", "lr"):
                address = values[name]
                trace.add_row(
                    f"Saved {name.upper()}", Text(f"0x{address:08X} {_location(address, image)}")
                )
            trace.add_row(
                "Limit", "Saved PC/LR evidence only; not a full unwind of an inactive thread."
            )
        stack.add_row("Sampled SP", f"0x{sp:08X}")
        capacity = bounds.top - bounds.start
        stack.add_row(
            "SP-based occupancy", f"{bounds.top - sp}/{capacity} bytes; not a high-water mark"
        )
        if config.get("CONFIG_INIT_STACKS") == "y":
            if capacity - (4 if config.get("CONFIG_STACK_SENTINEL") == "y" else 0) > MAX_STACK_SCAN:
                stack.add_row(
                    "Fill watermark", f"Unavailable: stack exceeds {MAX_STACK_SCAN}-byte budget"
                )
            else:
                unused, scanned = stack_high_water(
                    reader, bounds, config.get("CONFIG_STACK_SENTINEL") == "y"
                )
                stack.add_row(
                    "Fill watermark",
                    f"{scanned - unused}/{scanned} bytes touched; 0xAA pattern estimate, not proof",
                )
        else:
            stack.add_row("Fill watermark", "Unavailable: CONFIG_INIT_STACKS is disabled or absent")
        for name, value in values.items():
            registers.add_row(name, f"0x{value:08X}")
        return Group(stack, registers, trace)
    except (gdb.error, TargetReadError) as error:
        raise ValueError(f"Cannot read Zephyr stack/context: {error}") from error
