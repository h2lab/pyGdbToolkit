# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Bounded observations of the Cortex-M ready cache at exact PendSV entry."""

from collections.abc import Callable
from pathlib import Path
from typing import Mapping

import gdb
from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .base import SchedulerTrace
from .zephyr_threads import kernel_symbol, require_stopped, thread_name

MAX_OBSERVATIONS = 4096


def supports_trace(config: Mapping[str, str]) -> bool:
    """Return build eligibility; symbol/layout and target checks happen on activation."""
    return (
        config.get("CONFIG_CPU_CORTEX_M") == "y"
        and config.get("CONFIG_MP_MAX_NUM_CPUS") == "1"
        and not any(
            config.get(key) == "y"
            for key in (
                "CONFIG_SMP",
                "CONFIG_USE_SWITCH",
                "CONFIG_TRUSTED_EXECUTION_SECURE",
                "CONFIG_TRUSTED_EXECUTION_NONSECURE",
                "CONFIG_ARM_SECURE_FIRMWARE",
            )
        )
    )


def _pendsv_address(image: Path) -> int:
    try:
        with image.open("rb") as stream:
            elf = ELFFile(stream)
            symbols = elf.get_section_by_name(".symtab")
            if not isinstance(symbols, SymbolTableSection):
                raise ValueError("Zephyr scheduler observation requires an ELF symbol table")
            matches = [
                symbol
                for symbol in symbols.get_symbol_by_name("z_arm_pendsv") or ()
                if symbol["st_info"]["type"] == "STT_FUNC" and isinstance(symbol["st_shndx"], int)
            ]
            if len(matches) != 1:
                raise ValueError("Zephyr ELF requires one defined z_arm_pendsv function")
            symbol = matches[0]
            section = elf.get_section(symbol["st_shndx"])
            address = int(symbol["st_value"]) & ~1
            start, size = int(section["sh_addr"]), int(section["sh_size"])
            if (
                not section["sh_flags"] & 4
                or not 0 < address < 2**32
                or not start <= address < start + size
            ):
                raise ValueError("Zephyr z_arm_pendsv is not in a valid executable section")
            return address
    except (OSError, ELFError) as error:
        raise ValueError(f"Cannot decode Zephyr scheduler symbol: {error}") from error


class PendSvTrace(gdb.Breakpoint):
    """Own one exact-entry breakpoint, observations and target/image cleanup hooks."""

    def __init__(
        self,
        config: Mapping[str, str],
        image: Path,
        limit: int,
        console: Console,
        on_delete: Callable[[SchedulerTrace], None],
    ) -> None:
        if not 1 <= limit <= MAX_OBSERVATIONS:
            raise ValueError(f"Zephyr scheduler observation count must be 1..{MAX_OBSERVATIONS}")
        if not supports_trace(config):
            raise ValueError(
                "Zephyr PendSV observation requires Cortex-M, one configured CPU, "
                "without SMP, USE_SWITCH or TrustZone firmware"
            )
        require_stopped()
        self.config = dict(config)
        self.image = image
        self.limit = limit
        self.console = console
        self.on_delete = on_delete
        self.elections: list[str] = []
        self.active = True
        self._deleted = False
        self._breakpoint_deleting = False
        self._sample()
        address = _pendsv_address(image)
        super().__init__(f"*0x{address:X}", internal=True)
        gdb.events.exited.connect(self._target_exited)
        gdb.events.clear_objfiles.connect(self._target_exited)
        gdb.events.free_objfile.connect(self._objfile_freed)
        gdb.events.breakpoint_deleted.connect(self._breakpoint_deleted)

    def _thread(self, pointer: gdb.Value) -> str:
        address = int(pointer)
        size = pointer.type.target().sizeof
        if address <= 0 or address % 4 or size <= 0 or address + size > 2**32:
            raise ValueError(f"Invalid Zephyr scheduler thread address: 0x{address:X}")
        label = f"0x{address:08X}"
        if self.config.get("CONFIG_THREAD_NAME") == "y":
            name = thread_name(pointer.dereference())
            if name:
                label += f" ({name})"
        return label

    def _sample(self) -> str:
        kernel = kernel_symbol(self.image).value()
        cpus = kernel["cpus"]
        if cpus.type.strip_typedefs().range() != (0, 0):
            raise ValueError("Zephyr PendSV observation requires exactly one DWARF CPU slot")
        outgoing = self._thread(cpus[0]["current"])
        candidate = self._thread(kernel["ready_q"]["cache"])
        return f"{outgoing} -> {candidate}"

    def delete(self) -> None:
        """Release owned resources once, including callbacks for a cancelled trace."""
        if self._deleted:
            return
        self._deleted = True
        self.active = False
        gdb.events.exited.disconnect(self._target_exited)
        gdb.events.clear_objfiles.disconnect(self._target_exited)
        gdb.events.free_objfile.disconnect(self._objfile_freed)
        gdb.events.breakpoint_deleted.disconnect(self._breakpoint_deleted)
        if not self._breakpoint_deleting and self.is_valid():
            super().delete()
        self.on_delete(self)

    def _cancel(self, reason: str) -> None:
        if not self.active:
            return
        self.console.print(
            Text(f"Zephyr scheduler observation cancelled: {reason}", style="yellow")
        )
        self.delete()

    def _target_exited(self, event: object) -> None:
        del event
        self._cancel("target exited or symbols cleared")

    def _objfile_freed(self, event: gdb.FreeObjFileEvent) -> None:
        if event.objfile.filename == str(self.image):
            self._cancel("loaded Zephyr image removed")

    def _breakpoint_deleted(self, breakpoint: gdb.Breakpoint) -> None:
        if breakpoint is self:
            # GDB emits this before invalidating the breakpoint; do not delete it again.
            self._breakpoint_deleting = True
            self._cancel("observation breakpoint deleted")

    def stop(self) -> bool:
        """Record one pre-switch observation; stop at the exact limit or on failure."""
        if not self.active:
            return False
        try:
            observation = self._sample()
        except (gdb.error, ValueError) as error:
            self.active = False
            gdb.post_event(self.delete)
            self.console.print(Text(f"Cannot observe Zephyr scheduler: {error}", style="red"))
            return True
        self.elections.append(observation)
        if len(self.elections) < self.limit:
            return False
        self.active = False
        gdb.post_event(self.delete)
        table = Table(
            title="Zephyr PendSV entry observations (before switch)", box=box.SIMPLE_HEAVY
        )
        table.add_column("#")
        table.add_column("Current -> ready-cache candidate")
        for index, value in enumerate(self.elections, 1):
            table.add_row(str(index), Text(value))
        self.console.print(table)
        self.console.print(
            Text(
                "Stopped before PendSV performs this switch. Candidates can change after the "
                "sample; duplicates are preserved. Not confirmed execution, timing, CPU usage "
                "or an exhaustive scheduler history. Breakpoints perturb target timing."
            )
        )
        return True
