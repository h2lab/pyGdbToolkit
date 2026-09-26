"""Camelot RTOS project symbol locations."""

from dataclasses import dataclass
import json
from pathlib import Path
import struct
import tomllib

import gdb
from rich import box
from rich.console import Group
from rich.table import Table
from rich.text import Text

NAME = "camelot"


@dataclass(frozen=True)
class TaskSlot:
    index: int
    address: int
    task_name: str | None
    text_address: int
    ram_address: int
    metadata: dict[str, int | tuple[int, ...] | str]


@dataclass(frozen=True)
class TaskList:
    address: int
    size: int
    entry_size: int
    slots: tuple[TaskSlot, ...]


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
    byte_order = "little" if kernel_elf.read_bytes()[5] == 1 else "big"

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
                if field.name in ("task_hmac", "metadata_hmac"):
                    metadata[field.name] = raw.hex()
                elif field.type.code == gdb.TYPE_CODE_ARRAY:
                    item_size = field.type.target().sizeof
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

    private_build = project_path / "output" / "build" / "camelot_private"
    elfs = []
    if "kernel" in project:
        elfs.append(private_build / "sentry-kernel.elf")
    applications = project.get("application", {})
    if not isinstance(applications, dict):
        raise ValueError("project.toml: application must be a table")
    for name in applications:
        elfs.append(private_build / f"{name}-app.elf")
    if not elfs:
        raise ValueError("project.toml: no kernel or application declared")
    for elf in elfs:
        if not elf.is_file():
            raise FileNotFoundError(elf)
    return elfs


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

    output = [mapping_table("Kernel", owners.pop("kernel"))]
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
