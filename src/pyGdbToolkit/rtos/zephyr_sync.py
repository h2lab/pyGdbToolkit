# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Read-only Zephyr wait evidence and explicitly typed synchronization objects."""

from pathlib import Path
import re
from typing import Mapping

import gdb
from rich import box
from rich.console import Group
from rich.table import Table
from rich.text import Text

MAX_WAITERS = 4096
_TYPES = {"sem": "struct k_sem", "mutex": "struct k_mutex", "event": "struct k_event"}


def _address(address: int, size: int = 1) -> int:
    if address <= 0 or address % 4 or size <= 0 or address + size > 2**32:
        raise ValueError(f"Invalid Zephyr synchronization address: 0x{address:X}")
    return address


def _u32(value: gdb.Value) -> int:
    number = int(value)
    if not 0 <= number < 2**32:
        raise ValueError("Zephyr synchronization field is not an unsigned 32-bit value")
    return number


def _location(value: gdb.Value) -> int:
    address = value.address
    if address is None:
        raise ValueError("Zephyr DWARF value has no memory address")
    return _address(int(address), value.type.sizeof)


def _image_type(name: str, symbol: gdb.Symbol, image: Path) -> gdb.Type:
    try:
        kind = gdb.lookup_type(name, symbol.symtab.global_block())
    except gdb.error:
        kind = gdb.lookup_type(name)
    owner = kind.objfile
    if owner is None or owner.filename != str(image):
        raise ValueError(f"Zephyr DWARF type {name} is not owned by the loaded image: {image}")
    return kind


def show_thread_wait(thread: gdb.Value, state: int) -> Group:
    """Render a pending thread's queue pointer without guessing its containing object.

    Parameters
    ----------
    thread : gdb.Value
        Sampled k_thread from the loaded image.
    state : int
        Already decoded thread_state bitmask.

    Returns
    -------
    Group
        Raw wait evidence, without interpreting stale event fields or timeout deltas.

    Raises
    ------
    ValueError
        If required pending-thread data is unreadable or has an invalid pointer.
    """
    if state & 0x09:
        return Group(Text("Wait evidence unavailable: dummy or terminated thread."))
    if not state & 0x02:
        reason = "sleeping flag set" if state & 0x04 else "pending flag clear"
        return Group(Text(f"No object wait reported: {reason}. Remaining timeout unavailable."))
    try:
        queue = int(thread["base"]["pended_on"])
        if not queue:
            return Group(
                Text("Pending with no wait queue; object identity and timeout unavailable.")
            )
        _address(queue)
        return Group(
            Text(f"Pending wait queue: 0x{queue:08X} (base.pended_on)."),
            Text(
                "Object type/address unavailable from the queue pointer alone. "
                "Use rtos show object <sem|mutex|event> <hex-address> for a known object. "
                "Remaining timeout unavailable; timeout deltas are not remaining durations."
            ),
        )
    except gdb.error as error:
        raise ValueError(f"Cannot read Zephyr thread wait evidence: {error}") from error


def _waiters(queue: gdb.Value, thread_type: gdb.Type, event: bool) -> Table:
    head = queue["waitq"]
    head_address = _location(head)
    queue_address = _location(queue)
    # Computing member addresses on an artificial pointer uses DWARF, not target reads.
    probe = gdb.Value(0x1000).cast(thread_type.pointer()).dereference()
    offset = _location(probe["base"]["qnode_dlist"]) - 0x1000
    if not 0 <= offset or offset + head.type.sizeof > thread_type.sizeof:
        raise ValueError("Zephyr DWARF: invalid thread wait-node offset")
    table = Table(title="Wait queue (halted sample)", box=box.SIMPLE_HEAVY)
    for column in ("k_thread", "Priority", "State (raw)"):
        table.add_column(column)
    if event:
        table.add_column("Requested events")
        table.add_column("Wait mode / options (raw)")
    cursor = int(head["next"])
    previous = head_address
    visited: set[int] = set()
    while cursor != head_address:
        _address(cursor, head.type.sizeof)
        if cursor in visited:
            raise ValueError(f"Cycle in Zephyr wait queue at 0x{cursor:08X}")
        if len(visited) >= MAX_WAITERS:
            raise ValueError(f"Zephyr wait queue exceeds {MAX_WAITERS} entries")
        visited.add(cursor)
        node = gdb.Value(cursor).cast(head.type.pointer()).dereference()
        if int(node["prev"]) != previous:
            raise ValueError("Inconsistent Zephyr wait queue previous link")
        address = _address(cursor - offset, thread_type.sizeof)
        thread = gdb.Value(address).cast(thread_type.pointer()).dereference()
        base = thread["base"]
        state = int(base["thread_state"])
        if not 0 <= state <= 0xFF or not state & 0x02 or state & 0x08:
            raise ValueError("Zephyr wait queue contains a non-pending or terminated thread")
        if int(base["pended_on"]) != queue_address:
            raise ValueError("Zephyr wait queue disagrees with thread base.pended_on")
        priority = int(base["prio"])
        if not -128 <= priority <= 127:
            raise ValueError("Invalid Zephyr waiter priority")
        row = [f"0x{address:08X}", str(priority), f"0x{state:02X}"]
        if event:
            mask = _u32(thread["events"])
            options = _u32(thread["event_options"])
            row.extend((f"0x{mask:08X}", f"{'all' if options & 1 else 'any'} / 0x{options:08X}"))
        table.add_row(*row)
        previous, cursor = cursor, int(node["next"])
    if int(head["prev"]) != previous:
        raise ValueError("Inconsistent Zephyr wait queue tail link")
    table.caption = f"{len(visited)} waiter(s); queue order, not a scheduling prediction."
    return table


def show_object(kind: str, address: str, config: Mapping[str, str], image: Path) -> Group:
    """Inspect an explicitly typed object and its bounded simple-list wait queue.

    Parameters
    ----------
    kind : str
        ``sem``, ``mutex`` or ``event``.
    address : str
        Hexadecimal address of the complete object, never evaluated as an expression.
    config : Mapping[str, str]
        Effective Kconfig of the loaded build.
    image : Path
        ELF whose _kernel compilation unit anchors lookup; resolved types must belong to it.

    Returns
    -------
    Group
        Object fields, waiters or explicit unsupported-queue coverage.

    Raises
    ------
    ValueError
        If input, DWARF, memory, object invariants or queue links are invalid.
    """
    from .zephyr_threads import kernel_symbol, require_stopped

    if kind not in _TYPES:
        raise ValueError("Zephyr object type must be sem, mutex or event")
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", address) is None:
        raise ValueError("Zephyr object address must be hexadecimal, not a GDB expression")
    location = _address(int(address, 16))
    if kind == "event" and config.get("CONFIG_EVENTS") != "y":
        raise ValueError("Zephyr event inspection requires CONFIG_EVENTS=y")
    require_stopped()
    try:
        symbol = kernel_symbol(image)
        object_type = _image_type(_TYPES[kind], symbol, image)
        _address(location, object_type.sizeof)
        value = gdb.Value(location).cast(object_type.pointer()).dereference()
        table = Table(title=f"Zephyr {kind} at 0x{location:08X}", box=box.SIMPLE_HEAVY)
        table.add_column("Field")
        table.add_column("Value")
        if kind == "sem":
            count, limit = _u32(value["count"]), _u32(value["limit"])
            if not limit or count > limit:
                raise ValueError("Invalid Zephyr semaphore count/limit")
            table.add_row("Count / limit", f"{count} / {limit}")
        elif kind == "mutex":
            owner, count = int(value["owner"]), _u32(value["lock_count"])
            if owner:
                _address(owner)
            if bool(owner) != bool(count):
                raise ValueError("Inconsistent Zephyr mutex owner/lock_count")
            table.add_row("Owner k_thread", f"0x{owner:08X}" if owner else "none")
            table.add_row("Recursive lock count", str(count))
        else:
            table.add_row("Posted events", f"0x{_u32(value['events']):08X}")
        queue = value["wait_q"]
        table.add_row("wait_q", f"0x{_location(queue):08X}")
        if config.get("CONFIG_WAITQ_SCALABLE") == "y":
            waiters: Table | Text = Text(
                "Waiter enumeration unavailable: CONFIG_WAITQ_SCALABLE tree is not supported."
            )
        elif config.get("CONFIG_WAITQ_SIMPLE") != "y" and config.get("CONFIG_WAITQ_DUMB") != "y":
            waiters = Text(
                "Waiter enumeration unavailable: simple-list Kconfig is disabled or not recorded."
            )
        else:
            thread_type = _image_type("struct k_thread", symbol, image)
            waiters = _waiters(queue, thread_type, kind == "event")
        return Group(
            table,
            waiters,
            Text(
                "Type/address supplied by the caller; allocation, initialization and lifetime "
                "are not verified. No global object discovery, remaining timeout, wait history "
                "or deadlock proof. A halt inside a kernel update may expose inconsistent data."
            ),
        )
    except gdb.error as error:
        raise ValueError(f"Cannot read Zephyr synchronization object or DWARF: {error}") from error
