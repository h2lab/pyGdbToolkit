# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Camelot RTOS project symbol locations."""

from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
import struct
import tomllib
from typing import Literal
import xml.etree.ElementTree as ET

import gdb
from rich import box
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from ..shared.rtos import TaskListSnapshot, camelot as project_contract
from .base import RtosCapabilities, SchedulerTrace

NAME = project_contract.NAME


@dataclass(frozen=True)
class TaskSlot:
    """One task metadata slot and its resolved application name."""

    index: int
    address: int
    task_name: str | None
    text_address: int
    ram_address: int
    metadata: dict[str, int | tuple[int, ...] | str]


@dataclass(frozen=True)
class TaskList:
    """Location and decoded slots of the kernel's task metadata section."""

    address: int
    size: int
    entry_size: int
    slots: tuple[TaskSlot, ...]


@dataclass(frozen=True)
class CamelotProject:
    """Bind cached Camelot metadata to its build root and live operations."""

    path: Path
    task_list: TaskList
    capabilities: RtosCapabilities = RtosCapabilities(task_inspection=True, scheduler_trace=True)

    def task_list_snapshot(self) -> TaskListSnapshot:
        """Serialize cached Camelot slots without reading files or target memory."""
        return task_list_snapshot(self.task_list)

    def show_project(self) -> Group:
        """Render the Camelot build layout and static task metadata."""
        return show_project(self.path, self.task_list)

    def show_task(self, name: str) -> Group:
        """Inspect a Camelot task on the stopped target."""
        return show_task(self.path, self.task_list, name)

    def show_threads(self) -> Group:
        """Reject thread enumeration; Camelot exposes its static tasks through show."""
        raise ValueError("Camelot thread enumeration is unavailable; use rtos show")

    def trace_scheduler(
        self, limit: int, console: Console, on_delete: Callable[[SchedulerTrace], None]
    ) -> SchedulerTrace:
        """Observe Camelot elections after sched_elect returns."""
        return _SchedulerTrace(self.task_list, limit, console, on_delete)


class CamelotRtos:
    """Adapt Camelot's symbol discovery and static metadata to the RTOS runtime."""

    name = NAME

    def project_elfs(self, project_path: Path) -> list[Path]:
        """Require the kernel before any Camelot project symbols are loaded."""
        elfs = project_elfs(project_path)
        if not elfs or elfs[0].name != "sentry-kernel.elf":
            raise ValueError("kernel ELF is required")
        return elfs

    def load_project(self, project_path: Path, primary_elf: Path) -> CamelotProject:
        """Decode Camelot's static task table after its DWARF symbols are loaded."""
        return CamelotProject(project_path, load_task_list(project_path, primary_elf))


class _SchedulerTrace(gdb.Breakpoint):
    """Own Camelot's election breakpoint and all outstanding return breakpoints."""

    def __init__(
        self,
        task_list: TaskList,
        limit: int,
        console: Console,
        on_delete: Callable[[SchedulerTrace], None],
    ) -> None:
        if limit < 1:
            raise ValueError("scheduler trace limit must be positive")
        self.task_list = task_list
        self.limit = limit
        self.console = console
        self.on_delete = on_delete
        self.elections: list[str] = []
        self.returns: set[gdb.FinishBreakpoint] = set()
        self.active = True
        self._deleted = False
        super().__init__("sched_elect", internal=True)

    def delete(self) -> None:
        """Release entry and return breakpoints before notifying the session."""
        if self._deleted:
            return
        self.active = False
        for breakpoint in tuple(self.returns):
            if breakpoint.is_valid():
                breakpoint.delete()
        self.returns.clear()
        if self.is_valid():
            super().delete()
        self._deleted = True
        self.on_delete(self)

    def stop(self) -> bool:
        """Place a return breakpoint without stopping the target."""
        if not self.active:
            return False
        try:
            self.returns.add(_ElectionReturn(self))
        except gdb.error as error:
            self.active = False
            gdb.post_event(self.delete)
            self.console.print(f"[red]Cannot trace scheduler return: {error}[/red]")
            return True
        return False


class _ElectionReturn(gdb.FinishBreakpoint):
    """Capture the elected handle only while its owning Camelot trace is active."""

    def __init__(self, trace: _SchedulerTrace) -> None:
        self.trace = trace
        super().__init__(gdb.newest_frame(), internal=True)

    def stop(self) -> bool:
        """Record the returned task and stop after exactly the requested count."""
        trace = self.trace
        trace.returns.discard(self)
        if not trace.active:
            return False
        try:
            value = self.return_value
            handle = int(value if value is not None else gdb.parse_and_eval("$r0"))
            task = elected_task(trace.task_list, handle)
        except (gdb.error, ValueError) as error:
            trace.active = False
            gdb.post_event(trace.delete)
            trace.console.print(f"[red]Cannot read elected task: {error}[/red]")
            return True
        trace.elections.append(task)
        if len(trace.elections) < trace.limit:
            return False
        trace.active = False
        gdb.post_event(trace.delete)
        trace.console.print(scheduling_chart(trace.elections))
        return True

    def out_of_scope(self) -> None:
        """Cancel explicitly if GDB can no longer observe the scheduler return."""
        trace = self.trace
        trace.returns.discard(self)
        if trace.active:
            trace.active = False
            gdb.post_event(trace.delete)
            trace.console.print("[red]Cannot trace scheduler return: frame left scope[/red]")


def task_list_section(kernel_elf: Path) -> tuple[int, bytes]:
    """Read the linked address and contents of the kernel's task metadata section."""
    data = kernel_elf.read_bytes()
    if data[:4] != b"\x7fELF" or data[4] not in (1, 2) or data[5] not in (1, 2):
        raise ValueError(f"Not a supported ELF: {kernel_elf}")
    byte_order = "<" if data[5] == 1 else ">"
    is_64_bit = data[4] == 2
    header_format = byte_order + ("HHIQQQIHHHHHH" if is_64_bit else "HHIIIIIHHHHHH")
    header = struct.unpack_from(header_format, data, 16)
    section_offset, section_size, section_count, names_index = (
        header[5],
        header[10],
        header[11],
        header[12],
    )
    section_format = byte_order + ("IIQQQQIIQQ" if is_64_bit else "IIIIIIIIII")
    if section_size < struct.calcsize(section_format) or names_index >= section_count:
        raise ValueError(f"Invalid ELF section table: {kernel_elf}")
    sections = [
        struct.unpack_from(section_format, data, section_offset + index * section_size)
        for index in range(section_count)
    ]
    names = sections[names_index]
    name_table = data[names[4] : names[4] + names[5]]
    for section in sections:
        name = name_table[section[0] :].split(b"\0", 1)[0]
        if name == b".task_list":
            address, offset, size = section[3:6]
            if offset + size > len(data):
                raise ValueError(f"Truncated .task_list section: {kernel_elf}")
            return address, data[offset : offset + size]
    raise ValueError(f"No .task_list section in {kernel_elf}")


def load_task_list(project_path: Path, kernel_elf: Path) -> TaskList:
    """Decode the task slots using the generated C type's DWARF layout."""
    header = project_path / "output/build/kernel/kernel/include/sentry/managers/task_metadata.h"
    if not header.is_file():
        raise FileNotFoundError(header)
    address, contents = task_list_section(kernel_elf)
    task_type = gdb.lookup_type("task_meta_t")
    entry_size = task_type.sizeof
    if not entry_size or len(contents) % entry_size:
        raise ValueError(".task_list size is not a multiple of task_meta_t")
    fields = {field.name: field for field in task_type.fields()}
    if not {"s_text", "s_svcexchange"} <= fields.keys():
        raise ValueError("task_meta_t is missing task memory addresses")
    byte_order: Literal["little", "big"] = "little" if kernel_elf.read_bytes()[5] == 1 else "big"

    with (project_path / "output/build/camelot_private/layout.json").open() as layout_file:
        regions = json.load(layout_file)["regions"]
    text_regions = {
        int(region["start_address"], 0): region["name"]
        for region in regions
        if region["type"] == "text" and region["name"] != "kernel"
    }
    ram_regions = {
        region["name"]: int(region["start_address"], 0)
        for region in regions
        if region["type"] == "ram"
    }

    def field_bytes(entry: bytes, field: gdb.Field) -> bytes:
        if field.bitpos is None or field.type is None or field.name is None:
            raise ValueError("Incomplete task_meta_t field in debug symbols")
        if field.bitpos % 8:
            raise ValueError(f"Unaligned task_meta_t field: {field.name}")
        offset = field.bitpos // 8
        return entry[offset : offset + field.type.sizeof]

    def field_value(entry: bytes, name: str) -> int:
        return int.from_bytes(field_bytes(entry, fields[name]), byte_order)

    slots = []
    for index in range(len(contents) // entry_size):
        entry = contents[index * entry_size : (index + 1) * entry_size]
        text_address = field_value(entry, "s_text")
        ram_address = field_value(entry, "s_svcexchange")
        name = text_regions.get(text_address) if any(entry) else None
        if any(entry) and (name is None or ram_regions.get(name) != ram_address):
            raise ValueError(f"Cannot match .task_list slot {index} to a task in layout.json")
        metadata: dict[str, int | tuple[int, ...] | str] = {}
        if name is not None:
            for field in task_type.fields():
                raw = field_bytes(entry, field)
                if field.name is None or field.type is None:
                    raise ValueError("Incomplete task_meta_t field in debug symbols")
                if field.name in ("task_hmac", "metadata_hmac"):
                    metadata[field.name] = raw.hex()
                elif field.type.code == gdb.TYPE_CODE_ARRAY:
                    item_type = field.type.target()
                    if item_type is None:
                        raise ValueError(f"Unknown element type for {field.name}")
                    item_size = item_type.sizeof
                    metadata[field.name] = tuple(
                        int.from_bytes(raw[offset : offset + item_size], byte_order)
                        for offset in range(0, len(raw), item_size)
                    )
                else:
                    metadata[field.name] = int.from_bytes(raw, byte_order)
        slots.append(
            TaskSlot(index, address + index * entry_size, name, text_address, ram_address, metadata)
        )
    return TaskList(address, len(contents), entry_size, tuple(slots))


def project_elfs(project_path: Path) -> list[Path]:
    """Return the linked kernel and application ELF files declared by a project."""
    with (project_path / "project.toml").open("rb") as project_file:
        project = tomllib.load(project_file)

    elfs = [project_path / relative for relative in project_contract.project_elf_paths(project)]
    for elf in elfs:
        if not elf.is_file():
            raise FileNotFoundError(elf)
    return elfs


def task_list_snapshot(task_list: object) -> TaskListSnapshot:
    """Serialize cached build-time metadata without accessing the target or files."""
    if not isinstance(task_list, TaskList):
        raise ValueError("Invalid Camelot task metadata")
    return {
        "address": task_list.address,
        "size": task_list.size,
        "entry_size": task_list.entry_size,
        "slots": [
            {
                "index": slot.index,
                "address": slot.address,
                "task_name": slot.task_name,
                "text_address": slot.text_address,
                "ram_address": slot.ram_address,
                "metadata": {
                    name: list(value) if isinstance(value, tuple) else value
                    for name, value in slot.metadata.items()
                },
            }
            for slot in task_list.slots
        ],
    }


def elected_task(task_list: TaskList, handle: int) -> str:
    """Resolve a live scheduler handle through the kernel task table."""
    task_type = gdb.lookup_type("task_t")
    handle_field = next(field for field in task_type.fields() if field.name == "handle")
    if handle_field.bitpos is None or handle_field.type is None:
        raise ValueError("Incomplete task handle in debug symbols")
    if handle_field.bitpos % 8:
        raise ValueError("Unaligned task handle in task_t")
    table = int(gdb.parse_and_eval("&task_table"))
    inferior = gdb.selected_inferior()
    for index in range(len(task_list.slots) + 1):
        entry = table + index * task_type.sizeof
        stored = int.from_bytes(
            inferior.read_memory(entry + handle_field.bitpos // 8, handle_field.type.sizeof),
            "little",
        )
        if stored != handle:
            continue
        task_address = gdb.Value(entry)
        if task_address is None:
            raise ValueError("Cannot resolve task table address")
        task = task_address.cast(task_type.pointer()).dereference()
        label = int(task["metadata"].dereference()["label"])
        if label == 0xCAFE:
            return "idle"
        for slot in task_list.slots:
            if slot.task_name is not None and slot.metadata["label"] == label:
                return slot.task_name
        return f"unknown (label 0x{label:X})"
    return f"unknown (handle 0x{handle:X})"


def scheduling_chart(elections: list[str]) -> Group:
    """Render chronological election windows without exceeding terminal width."""
    charts = []
    for start in range(0, len(elections), 8):
        window = elections[start : start + 8]
        chart = Table(
            title=f"Task scheduling: elections {start + 1}-{start + len(window)}",
            box=box.SIMPLE_HEAVY,
            header_style="bold cyan",
        )
        chart.add_column("Task", style="bold", no_wrap=True)
        for index in range(start, start + len(window)):
            chart.add_column(str(index + 1), justify="center", no_wrap=True)
        for task in dict.fromkeys(elections):
            chart.add_row(task, *("●" if elected == task else "·" for elected in window))
        charts.append(chart)
    return Group(*charts)


def _interrupt_names() -> dict[int, str]:
    """Resolve IRQ numbers from the SVD currently loaded by the svd command."""
    from ..cmd_svd import SESSION
    from ..svd import _parse_int

    if SESSION.svd_path is None:
        return {}
    root = ET.parse(SESSION.svd_path).getroot()
    names: dict[int, str] = {}
    for peripheral in root.iter():
        if peripheral.tag.rsplit("}", 1)[-1] != "peripheral":
            continue
        for interrupt in peripheral:
            if interrupt.tag.rsplit("}", 1)[-1] != "interrupt":
                continue
            children = {child.tag.rsplit("}", 1)[-1]: child.text for child in interrupt}
            name = children.get("name")
            value = children.get("value")
            if name and value:
                names.setdefault(_parse_int(value), name)
    return names


def _task_symbol(address: int, text_start: int, text_end: int) -> str | None:
    """Resolve a Thumb address only within the selected task's text range."""
    code_address = address & ~1
    if not text_start <= code_address < text_end:
        return None
    try:
        symbol = gdb.execute(f"info symbol 0x{code_address:X}", to_string=True).strip()
    except gdb.error:
        return None
    if symbol.startswith("No symbol matches"):
        return None
    return symbol.split(" in section ", 1)[0]


def show_task(project_path: Path, task_list: TaskList, task_name: str) -> Group:
    """Inspect a named task's live kernel context on a stopped target."""
    layout_path = project_path / "output/build/camelot_private/layout.json"
    with layout_path.open(encoding="utf-8") as layout_file:
        regions = [r for r in json.load(layout_file)["regions"] if r["name"] == task_name]
    if not regions:
        raise ValueError(f"Unknown task: {task_name}")
    slot = next((s for s in task_list.slots if s.task_name == task_name), None)
    task_type = gdb.lookup_type("task_t")
    table_address = int(gdb.parse_and_eval("&task_table"))
    source_names: dict[int, str] = {}
    selected = None
    for index in range(len(task_list.slots) + 1):
        task_address = gdb.Value(table_address + index * task_type.sizeof)
        if task_address is None:
            raise ValueError("Cannot resolve task table address")
        task = task_address.cast(task_type.pointer()).dereference()
        metadata_address = int(task["metadata"])
        if not metadata_address:
            continue
        label = int(task["metadata"].dereference()["label"])
        source_name = next(
            (
                candidate.task_name
                for candidate in task_list.slots
                if candidate.task_name is not None and candidate.metadata.get("label") == label
            ),
            "idle" if label == 0xCAFE else f"label 0x{label:X}",
        )
        source_names[index] = source_name
        if (slot is not None and metadata_address == slot.address) or (
            task_name == "idle" and label == 0xCAFE
        ):
            selected = task
    if selected is None:
        raise ValueError(f"Task {task_name} is not initialized in task_table")

    def table(title: str, columns: tuple[str, ...]) -> Table:
        result = Table(title=title, box=box.SIMPLE_HEAVY, header_style="bold cyan")
        for column in columns:
            result.add_column(column)
        return result

    mapping = table(f"Task: {task_name}", ("Region", "Address range", "Access"))
    ram_start = ram_end = 0
    text_start = text_end = 0
    for region in regions:
        start = int(region["start_address"], 0)
        end = start + int(region["size"], 0)
        if region["type"] == "ram":
            ram_start, ram_end = start, end
        if region["type"] == "text":
            text_start, text_end = start, end
        permission = int(region["permission"])
        access = "".join(
            flag if permission & bit else "-" for bit, flag in ((1, "R"), (2, "W"), (4, "X"))
        )
        mapping.add_row(region["type"], f"0x{start:08X}-0x{end:08X}", access)

    state_number = int(selected["state"])
    state = next(
        (
            field.name
            for field in gdb.lookup_type("job_state_t").fields()
            if field.enumval == state_number
        ),
        f"unknown ({state_number})",
    )
    context = table("Kernel context", ("Field", "Value"))
    context.add_row("State", state)
    handle_address = selected["handle"].address
    if handle_address is None:
        raise ValueError("Cannot resolve task handle address")
    context.add_row(
        "Handle",
        f"0x{int(handle_address.cast(gdb.lookup_type('uint32_t').pointer()).dereference()):08X}",
    )
    saved_sp = int(selected["sp"])
    context.add_row("Saved SP", f"0x{saved_sp:08X}")

    events = table("Pending events", ("Type", "Source", "Value"))
    for field_name in ("ipcs", "sigs"):
        for index in range(
            selected[field_name].type.sizeof // selected[field_name].type.target().sizeof
        ):
            value = int(selected[field_name][index])
            if value:
                events.add_row(
                    "IPC" if field_name == "ipcs" else "Signal",
                    source_names.get(index, f"task #{index}"),
                    str(value),
                )
    irq_names = _interrupt_names()
    fields = {field.name for field in task_type.fields()}

    def queued(field: str, head: str, bottom: str) -> list[gdb.Value]:
        values = selected[field]
        length = values.type.sizeof // values.type.target().sizeof
        cursor, stop = int(selected[bottom]), int(selected[head])
        if cursor >= length or stop >= length:
            raise ValueError(f"Invalid {field} queue indices")
        items = []
        while cursor != stop:
            items.append(values[cursor])
            cursor = (cursor + 1) % length
        return items

    for event in queued("ints", "ints_head", "ints_bottom"):
        number = int(event)
        events.add_row("IRQ", str(number), irq_names.get(number, "unresolved"))
    if {"dmas", "dmas_head", "dmas_bottom"} <= fields:
        for event in queued("dmas", "dmas_head", "dmas_bottom"):
            events.add_row("DMA", f"0x{int(event['handle']):X}", str(event["event"]))
    if not events.rows:
        events.add_row("None", "", "")

    stack = table("Stack", ("Measure", "Value"))
    metadata = selected["metadata"].dereference()
    stack_size = int(metadata["stack_size"])
    config_paths = list(
        (project_path / "output/build/kernel/subprojects").glob("kconfig-*/generated_kconfig.json")
    )
    if len(config_paths) != 1:
        raise ValueError("Expected one generated kernel Kconfig file")
    config_path = config_paths[0]
    with config_path.open(encoding="utf-8") as config_file:
        config = json.load(config_file)
    svc_size = int(config["CONFIG_SVC_EXCHANGE_AREA_LEN"])
    stack_bottom = (
        ram_start
        + svc_size
        + sum(
            (int(metadata[field]) + 3) & ~3
            for field in ("got_size", "data_size", "bss_size", "heap_size")
        )
    )
    stack_top = stack_bottom + ((stack_size + 3) & ~3)
    pc = int(gdb.selected_frame().pc())
    current = text_start <= pc < text_end
    stack_pointer = int(gdb.parse_and_eval("$psp")) if current else saved_sp
    if not ram_start or stack_top > ram_end or not stack_bottom <= stack_pointer <= stack_top:
        stack.add_row("Usage", "Unavailable (saved SP outside stack bounds)")
    else:
        used = stack_top - stack_pointer
        stack.add_row("Bounds", f"0x{stack_bottom:08X}-0x{stack_top:08X}")
        stack.add_row("SP", f"0x{stack_pointer:08X} ({'live PSP' if current else 'saved'})")
        stack.add_row("Usage", f"{used}/{stack_size} bytes ({used / stack_size:.1%})")

    frames = table("Backtrace", ("Frame", "Location"))
    # GDB can unwind the running task only when it is stopped in its own code.
    if current:
        for line in gdb.execute("bt", to_string=True).splitlines():
            frames.add_row("GDB", line)
    elif stack_bottom <= saved_sp < stack_top and task_name != "idle":
        frame = selected["sp"].dereference()
        for field in ("pc", "prev_lr", "lr"):
            address = int(frame[field])
            symbol = _task_symbol(address, text_start, text_end)
            frames.add_row(
                f"saved {field}", f"0x{address:08X}  {symbol}" if symbol else f"0x{address:08X}"
            )
        frames.add_row(
            "Note", "Full unwinding of an inactive task requires its register context in GDB"
        )
    else:
        frames.add_row("Note", "No saved frame available for this task")
    return Group(mapping, context, stack, events, frames)


def show_project(project_path: Path, task_list: TaskList) -> Group:
    """Render the kernel, task mappings and decoded build-time metadata."""
    layout_path = project_path / "output" / "build" / "camelot_private" / "layout.json"
    with layout_path.open(encoding="utf-8") as layout_file:
        layout = json.load(layout_file)
    regions = layout["regions"]
    if not isinstance(regions, list):
        raise ValueError("layout.json: regions must be a list")

    owners: dict[str, list[tuple[str, str, str]]] = {}
    for region in regions:
        name = region["name"]
        start = int(region["start_address"], 0)
        size = int(region["size"], 0)
        permission = int(region["permission"])
        access = "".join(
            flag if permission & bit else "-" for bit, flag in ((1, "R"), (2, "W"), (4, "X"))
        )
        owners.setdefault(name, []).append(
            (region["type"], f"0x{start:08X}-0x{start + size:08X}", access)
        )

    if "kernel" not in owners:
        raise ValueError("layout.json: kernel mapping is missing")

    def mapping_table(title: str, mapping: list[tuple[str, str, str]]) -> Table:
        table = Table(title=Text(title), box=box.SIMPLE_HEAVY, header_style="bold cyan")
        table.add_column("Region", style="bold")
        table.add_column("Address range")
        table.add_column("Access")
        for region_type, address_range, access in mapping:
            table.add_row(region_type, address_range, access)
        return table

    slots_by_name: dict[str, list[TaskSlot]] = {}
    for slot in task_list.slots:
        if slot.task_name is not None:
            slots_by_name.setdefault(slot.task_name, []).append(slot)

    output: list[Table | Text] = [mapping_table("Kernel", owners.pop("kernel"))]
    output.append(
        Text(
            f".task_list: 0x{task_list.address:08X}-0x{task_list.address + task_list.size:08X} "
            f"({len(task_list.slots)} slots, {task_list.entry_size} bytes each)",
            style="cyan",
        )
    )
    for name, mapping in owners.items():
        output.append(mapping_table(f"Task: {name}", mapping))
        for slot in slots_by_name.get(name, []):
            table = Table(
                title=f"Metadata: {name} (slot {slot.index} at 0x{slot.address:08X})",
                box=box.SIMPLE_HEAVY,
                header_style="bold cyan",
            )
            table.add_column("Field", style="bold")
            table.add_column("Value", overflow="fold")
            for field_name, value in slot.metadata.items():
                if isinstance(value, tuple):
                    count = slot.metadata[f"num_{field_name}"]
                    rendered = ", ".join(f"0x{item:X}" for item in value)
                    if isinstance(count, int):
                        rendered += f" ({count} active)"
                elif isinstance(value, str):
                    rendered = value
                elif field_name in {
                    "magic",
                    "label",
                    "capabilities",
                    "flags",
                    "s_text",
                    "s_got",
                    "s_svcexchange",
                    "entrypoint_offset",
                    "finalize_offset",
                }:
                    rendered = f"0x{value:X}"
                else:
                    rendered = str(value)
                table.add_row(field_name, rendered)
            output.append(table)
        if name not in slots_by_name:
            output.append(Text("No task metadata in .task_list", style="dim"))
    return Group(*output)
