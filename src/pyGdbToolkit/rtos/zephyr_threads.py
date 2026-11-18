# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Bounded, DWARF-driven Zephyr thread inspection on a stopped target."""

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Mapping

import gdb
from rich import box
from rich.console import Group
from rich.table import Table
from rich.text import Text

from . import zephyr_context

MAX_THREADS = 4096
MAX_CPUS = 256
MAX_NAME_BYTES = 4096
_STATES = (
    (0x01, "dummy"),
    (0x02, "pending"),
    (0x04, "sleeping"),
    (0x08, "dead"),
    (0x10, "suspended"),
    (0x20, "aborting"),
    (0x40, "suspending"),
    (0x80, "queued"),
)


def supports_version(version: str) -> bool:
    """Return whether the declared release uses the supported Zephyr 4.1+ state ABI."""
    match = re.fullmatch(r"4\.([0-9]+)\.[0-9]+(?:[-+][A-Za-z0-9_.+-]+)?", version)
    return match is not None and int(match[1]) >= 1


@dataclass(frozen=True)
class ThreadInfo:
    """One sampled k_thread; current CPU roles do not imply execution outside an ISR."""

    address: int
    name: str | None
    priority: int
    state: int
    options: int
    roles: tuple[str, ...]
    entry: int | None
    value: gdb.Value | None = field(default=None, repr=False, compare=False)
    current: bool = False

    @property
    def state_text(self) -> str:
        """Decode Zephyr state bits without confusing queued and currently scheduled."""
        return " | ".join(label for mask, label in _STATES if self.state & mask) or "ready"


@dataclass(frozen=True)
class ThreadSnapshot:
    """A fresh halted-target sample with explicit enumeration and naming coverage."""

    threads: tuple[ThreadInfo, ...]
    notes: tuple[str, ...]
    names_available: bool
    cpu_count: int = 1


def _require_stopped() -> None:
    threads = gdb.selected_inferior().threads()
    if not threads or any(not thread.is_stopped() for thread in threads):
        raise ValueError(
            "Connect to a target and stop all its GDB threads before inspecting Zephyr"
        )


def _address(value: int, size: int) -> int:
    if value <= 0 or value % 4 or size <= 0 or value + size > 2**32:
        raise ValueError(f"Invalid Zephyr k_thread address: 0x{value:X}")
    return value


def _name(thread: gdb.Value) -> str | None:
    value = thread["name"]
    kind = value.type.strip_typedefs()
    if kind.code != gdb.TYPE_CODE_ARRAY or kind.target().sizeof != 1:
        raise ValueError("Zephyr DWARF: k_thread.name must be a byte array")
    first, last = kind.range()
    size = last - first + 1
    if first != 0 or not 0 < size <= MAX_NAME_BYTES:
        raise ValueError("Zephyr DWARF: invalid k_thread.name size")
    return value.string(encoding="utf-8", errors="replace", length=size).split("\0", 1)[0] or None


def read_threads(
    config: Mapping[str, str], extra_address: int | None = None, image: Path | None = None
) -> ThreadSnapshot:
    """Read current/idle threads and, when enabled, the monitored thread registry.

    Parameters
    ----------
    config : Mapping[str, str]
        Effective Kconfig of the loaded image.
    extra_address : int | None
        Explicit k_thread address to inspect even when thread monitoring is disabled.
    image : Path | None
        Loaded ELF whose symbols own the sample. None uses GDB's global lookup.

    Returns
    -------
    ThreadSnapshot
        Fresh observations, without caching live values or modifying the target.

    Raises
    ------
    ValueError
        If the target is running, data is unreadable, DWARF is incompatible or a
        list is corrupt. Partial enumeration without THREAD_MONITOR is labeled.
    """
    _require_stopped()
    try:
        if image is None:
            symbol = gdb.lookup_global_symbol("_kernel")
        else:
            objfile = next(
                (obj for obj in reversed(gdb.objfiles()) if obj.filename == str(image)), None
            )
            if objfile is None:
                raise ValueError(f"Zephyr symbol file is no longer loaded: {image}")
            symbol = objfile.lookup_global_symbol("_kernel")
        if symbol is None:
            raise ValueError("Zephyr DWARF: missing _kernel symbol")
        if symbol.symtab is None:
            raise ValueError("Zephyr DWARF: _kernel has no compilation unit")
        kernel = symbol.value()
        thread_type = gdb.lookup_type("struct k_thread", symbol.symtab.global_block())
        size = thread_type.sizeof
        cpus = kernel["cpus"]
        first, last = cpus.type.strip_typedefs().range()
        if first != 0 or not 0 < last + 1 <= MAX_CPUS:
            raise ValueError("Zephyr DWARF: invalid _kernel.cpus array bounds")
        roles: dict[int, list[str]] = {}
        current_addresses: set[int] = set()
        notes: list[str] = []
        for index in range(last + 1):
            cpu = cpus[index]
            nested = int(cpu["nested"])
            for member in ("current", "idle_thread"):
                address = int(cpu[member])
                if address:
                    _address(address, size)
                    if member == "current":
                        current_addresses.add(address)
                    role = f"{member} CPU {index}"
                    if member == "current" and nested:
                        role += f" (IRQ nesting {nested})"
                    roles.setdefault(address, []).append(role)
                else:
                    notes.append(f"CPU {index} {member} is null; kernel may not be initialized.")
        monitored = config.get("CONFIG_THREAD_MONITOR") == "y"
        names = config.get("CONFIG_THREAD_NAME") == "y"
        addresses: list[int] = []
        values: dict[int, gdb.Value] = {}

        def thread_at(address: int) -> gdb.Value:
            _address(address, size)
            if address not in values:
                values[address] = gdb.Value(address).cast(thread_type.pointer()).dereference()
            return values[address]

        if monitored:
            cursor = int(kernel["threads"])
            visited: set[int] = set()
            while cursor:
                if cursor in visited:
                    raise ValueError(f"Cycle in Zephyr monitored thread list at 0x{cursor:08X}")
                if len(visited) >= MAX_THREADS:
                    raise ValueError(f"Zephyr monitored thread list exceeds {MAX_THREADS} entries")
                visited.add(cursor)
                addresses.append(cursor)
                cursor = int(thread_at(cursor)["next_thread"])
            notes.append("Source: _kernel.threads (CONFIG_THREAD_MONITOR=y), sampled while halted.")
        else:
            notes.append(
                "Incomplete enumeration: CONFIG_THREAD_MONITOR is disabled or not recorded; "
                "only current/idle threads and an explicitly requested address are available."
            )
        for address in roles:
            if address not in addresses:
                if monitored:
                    notes.append(
                        f"CPU-referenced thread 0x{address:08X} is absent from monitor list."
                    )
                addresses.append(address)
        if extra_address is not None and extra_address not in addresses:
            _address(extra_address, size)
            addresses.append(extra_address)
            notes.append(
                f"Explicit address 0x{extra_address:08X}: thread membership is not verified."
            )
        if not names:
            notes.append(
                "Thread names unavailable: CONFIG_THREAD_NAME is disabled or not recorded."
            )
        result = []
        for address in addresses:
            thread = thread_at(address)
            base = thread["base"]
            state = int(base["thread_state"])
            if not 0 <= state <= 0xFF:
                raise ValueError("Zephyr DWARF: thread_state is not an 8-bit bitmask")
            result.append(
                ThreadInfo(
                    address=address,
                    name=_name(thread) if names else None,
                    priority=int(base["prio"]),
                    state=state,
                    options=int(base["user_options"]),
                    roles=tuple(roles.get(address, ())),
                    entry=int(thread["entry"]["pEntry"]) if monitored else None,
                    value=thread,
                    current=address in current_addresses,
                )
            )
        return ThreadSnapshot(tuple(result), tuple(notes), names, last + 1)
    except gdb.error as error:
        raise ValueError(f"Cannot read Zephyr thread data or DWARF: {error}") from error


def show_threads(
    config: Mapping[str, str],
    version: str,
    identifier: str | None = None,
    image: Path | None = None,
    context: bool = False,
) -> Group:
    """Render a fresh thread list or inspect an exact name/hexadecimal address.

    Parameters
    ----------
    config : Mapping[str, str]
        Loaded effective Kconfig.
    version : str
        Declared Zephyr version; live state decoding supports 4.1 through 4.x.
    identifier : str | None
        Exact thread name or hexadecimal k_thread address; None lists all threads.
    image : Path | None
        Loaded image whose symbols must be used instead of another project's.
    context : bool
        Whether a selected thread's Cortex-M stack/context should also be inspected.

    Returns
    -------
    Group
        Live identity, priority, raw state/options, CPU roles and available entry.

    Raises
    ------
    ValueError
        If target data is inaccessible or selection is invalid or ambiguous.
    """
    if not supports_version(version):
        raise ValueError(
            f"Zephyr live thread decoding requires version 4.1 through 4.x, got {version!r}"
        )
    address = None
    if identifier is not None and re.fullmatch(r"0[xX][0-9a-fA-F]+", identifier):
        address = _address(int(identifier, 16), 1)
    snapshot = read_threads(config, address, image)
    selected = snapshot.threads
    if identifier is not None:
        selected = tuple(
            thread
            for thread in selected
            if (thread.address == address if address is not None else thread.name == identifier)
        )
        if not selected:
            reason = (
                "names unavailable; use a hexadecimal k_thread address"
                if not snapshot.names_available
                else "no matching monitored/current/idle thread"
            )
            raise ValueError(f"Unknown Zephyr thread {identifier!r}: {reason}")
        if len(selected) != 1:
            candidates = ", ".join(f"0x{thread.address:08X}" for thread in selected)
            raise ValueError(
                f"Ambiguous Zephyr thread name {identifier!r}; use an address: {candidates}"
            )
    table = Table(title="Zephyr threads (halted sample)", box=box.SIMPLE_HEAVY)
    table.add_column("k_thread", min_width=10, no_wrap=True)
    for column in ("Name", "Priority", "State", "CPU role"):
        table.add_column(column, overflow="fold")
    for thread in selected:
        name = thread.name or ("<unnamed>" if snapshot.names_available else "<unavailable>")
        table.add_row(
            f"0x{thread.address:08X}",
            Text(name),
            str(thread.priority),
            f"{thread.state_text} (0x{thread.state:02X})",
            ", ".join(thread.roles) or "-",
        )
    details = Table(title="Thread details", box=box.SIMPLE_HEAVY)
    details.add_column("Field")
    details.add_column("Value", overflow="fold")
    if identifier is not None:
        thread = selected[0]
        details.add_row("User options (raw)", f"0x{thread.options:04X}")
        details.add_row(
            "Entry",
            f"0x{thread.entry:08X}" if thread.entry is not None else "unavailable (no monitor)",
        )
    blocks: list[Group | Text] = []
    if context and identifier is not None:
        thread = selected[0]
        if thread.value is None:
            raise ValueError("Zephyr thread context has no sampled DWARF value")
        if thread.state & 0x09:
            blocks.append(Text("Stack/context unavailable: dummy or terminated thread."))
        else:
            blocks.append(
                zephyr_context.show_context(
                    thread.value, thread.current, snapshot.cpu_count, config, image
                )
            )
    return Group(
        table,
        *((details,) if identifier is not None else ()),
        *(Text(note) for note in snapshot.notes),
        *blocks,
        Text(
            "Current means scheduled by the kernel, not necessarily executing outside an ISR. "
            "Wait-object resolution and scheduler tracing are not included."
        ),
    )
