# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Load Cortex-M Zephyr builds, display cached metadata and inspect live threads."""

from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
import re
import struct
from types import MappingProxyType
from typing import Mapping

from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection
from rich import box
from rich.console import Console, Group
from rich.table import Table
from rich.text import Text

from ..shared.rtos import TaskListSnapshot
from ..shared.rtos import zephyr as project_contract
from .base import RtosCapabilities, SchedulerTrace
from . import zephyr_sync, zephyr_threads, zephyr_trace

NAME = project_contract.NAME


def _configuration(path: Path) -> Mapping[str, str]:
    data = path.read_bytes()
    if len(data) > project_contract.MAX_METADATA_SIZE:
        raise ValueError("Zephyr .config exceeds the metadata size limit")
    try:
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise ValueError("Zephyr .config is not UTF-8") from error
    values: dict[str, str] = {}
    for line in text.splitlines():
        disabled = re.fullmatch(r"# (CONFIG_[A-Za-z0-9_]+) is not set", line)
        if disabled:
            key, value = disabled[1], "n"
        elif not line or line.startswith("#"):
            continue
        else:
            match = re.fullmatch(r"(CONFIG_[A-Za-z0-9_]+)=(.+)", line)
            if match is None:
                raise ValueError(f"Invalid Zephyr .config entry: {line!r}")
            key, value = match[1], match[2]
        if key in values:
            raise ValueError(f"Invalid or duplicate Zephyr .config entry: {line!r}")
        values[key] = value
    return MappingProxyType(values)


def _config_text(config: Mapping[str, str], key: str) -> str:
    try:
        value: object = json.loads(config[key])
    except (KeyError, ValueError) as error:
        raise ValueError(f"Zephyr .config: missing or invalid {key}") from error
    if not isinstance(value, str) or not value:
        raise ValueError(f"Zephyr .config: missing or invalid {key}")
    return value


@dataclass(frozen=True)
class LoadRegion:
    """One ELF PT_LOAD segment, not a physical memory capacity or MPU permission."""

    address: int
    size: int
    load_address: int
    file_size: int
    flags: int


def _elf_regions(path: Path) -> tuple[LoadRegion, ...]:
    try:
        with path.open("rb") as stream:
            elf = ELFFile(stream)
            if (
                elf["e_type"] != "ET_EXEC"
                or elf["e_machine"] != "EM_ARM"
                or elf.elfclass != 32
                or not elf.little_endian
            ):
                raise ValueError("Expected a linked 32-bit little-endian Arm Zephyr ELF")
            if not elf.has_dwarf_info(strict=True):
                raise ValueError("Zephyr ELF requires embedded DWARF debug information")
            if next(elf.get_dwarf_info().iter_CUs(), None) is None:
                raise ValueError("Zephyr ELF has no DWARF compilation units")
            symbols = elf.get_section_by_name(".symtab")
            if not isinstance(symbols, SymbolTableSection):
                raise ValueError("Zephyr ELF requires a symbol table")
            kernel = symbols.get_symbol_by_name("_kernel")
            if not kernel or all(symbol["st_shndx"] == "SHN_UNDEF" for symbol in kernel):
                raise ValueError("Zephyr ELF has no defined _kernel symbol")
            regions = []
            stream.seek(0, 2)
            file_size = stream.tell()
            for segment in elf.iter_segments():
                if segment["p_type"] != "PT_LOAD" or not segment["p_memsz"]:
                    continue
                address = int(segment["p_vaddr"])
                size = int(segment["p_memsz"])
                stored = int(segment["p_filesz"])
                if (
                    stored > size
                    or address + size > 2**32
                    or int(segment["p_offset"]) + stored > file_size
                ):
                    raise ValueError("Invalid or truncated Zephyr ELF load segment")
                regions.append(
                    LoadRegion(
                        address, size, int(segment["p_paddr"]), stored, int(segment["p_flags"])
                    )
                )
            if not regions:
                raise ValueError("Zephyr ELF has no loadable memory regions")
            return tuple(regions)
    except (ELFError, struct.error) as error:
        raise ValueError(f"Cannot decode Zephyr ELF: {error}") from error


@dataclass(frozen=True)
class ZephyrProject:
    """Cached identity, effective configuration and ELF mapping for one build."""

    build: project_contract.ZephyrBuild
    config: Mapping[str, str]
    regions: tuple[LoadRegion, ...]
    image: Path

    @property
    def capabilities(self) -> RtosCapabilities:
        """Enable thread decoding only for the supported kernel state ABI."""
        supported = zephyr_threads.supports_version(self.build.version)
        return RtosCapabilities(
            supported,
            supported and zephyr_trace.supports_trace(self.config),
            thread_enumeration=supported,
            object_inspection=supported,
        )

    def task_list_snapshot(self) -> TaskListSnapshot | None:
        """Report that Zephyr has no Camelot-style static task table."""
        return None

    def show_project(self) -> Group:
        """Render cached build information without rereading files or target memory."""
        identity = Table(title="Zephyr build", box=box.SIMPLE_HEAVY)
        identity.add_column("Field")
        identity.add_column("Value")
        for label, value in (
            ("Application", self.build.application),
            ("Zephyr", self.build.version),
            ("Board", self.build.board),
            ("Qualifiers", self.build.qualifiers or "(none)"),
            ("Architecture", "Arm Cortex-M (32-bit little-endian)"),
            ("Image", project_contract.KERNEL_ELF),
        ):
            identity.add_row(label, Text(value))
        memory = Table(title="Linked ELF segments (end-exclusive)", box=box.SIMPLE_HEAVY)
        for column in ("Runtime range", "Load address", "File bytes", "ELF flags"):
            memory.add_column(column)
        for region in self.regions:
            access = "".join(
                flag if region.flags & bit else "-" for bit, flag in ((4, "R"), (2, "W"), (1, "X"))
            )
            memory.add_row(
                f"0x{region.address:08X}-0x{region.address + region.size:08X}",
                f"0x{region.load_address:08X}",
                str(region.file_size),
                access,
            )
        options = Table(title="Build observability", box=box.SIMPLE_HEAVY)
        options.add_column("Configuration")
        options.add_column("Value")
        for key in (
            "CONFIG_THREAD_MONITOR",
            "CONFIG_THREAD_NAME",
            "CONFIG_DEBUG_THREAD_INFO",
            "CONFIG_THREAD_STACK_INFO",
            "CONFIG_INIT_STACKS",
            "CONFIG_SMP",
            "CONFIG_WAITQ_SIMPLE",
            "CONFIG_WAITQ_DUMB",
            "CONFIG_WAITQ_SCALABLE",
            "CONFIG_EVENTS",
            "CONFIG_MP_MAX_NUM_CPUS",
            "CONFIG_USE_SWITCH",
        ):
            options.add_row(key, self.config.get(key, "unavailable (not recorded)"))
        return Group(
            identity,
            memory,
            Text(
                "ELF flags are link-time attributes, not measured MPU permissions or memory capacity."
            ),
            options,
            Text(
                "Live thread inspection requires Zephyr 4.1 through 4.x and a stopped target "
                "(rtos show threads). "
                "rtos showsched observes ready-cache candidates at PendSV entry on supported "
                "mono-CPU Cortex-M builds; activation requires the handler symbol and layout."
            ),
        )

    def show_task(self, name: str) -> Group:
        """Inspect a Zephyr thread by exact name or hexadecimal k_thread address."""
        return zephyr_threads.show_threads(
            self.config, self.build.version, name, self.image, context=True
        )

    def show_threads(self) -> Group:
        """Enumerate observable threads with explicit monitor and name availability."""
        return zephyr_threads.show_threads(self.config, self.build.version, image=self.image)

    def show_object(self, kind: str, address: str) -> Group:
        """Inspect an explicitly typed semaphore, mutex or event and its wait queue."""
        if not zephyr_threads.supports_version(self.build.version):
            raise ValueError("Zephyr object inspection requires version 4.1 through 4.x")
        return zephyr_sync.show_object(kind, address, self.config, self.image)

    def trace_scheduler(
        self, limit: int, console: Console, on_delete: Callable[[SchedulerTrace], None]
    ) -> SchedulerTrace:
        """Observe bounded ready-cache candidates at Cortex-M PendSV entry."""
        if not zephyr_threads.supports_version(self.build.version):
            raise ValueError("Zephyr scheduler observation requires version 4.1 through 4.x")
        return zephyr_trace.PendSvTrace(self.config, self.image, limit, console, on_delete)


def _read_project(path: Path) -> ZephyrProject:
    for relative in project_contract.REQUIRED:
        if not (path / relative).is_file():
            raise FileNotFoundError(path / relative)
    build = project_contract.parse_build_info((path / project_contract.PROJECT_FILE).read_bytes())
    config = _configuration(path / project_contract.KCONFIG)
    if _config_text(config, "CONFIG_BOARD") != build.board:
        raise ValueError("Zephyr board mismatch between build_info.yml and .config")
    if config.get("CONFIG_CPU_CORTEX_M") != "y" or _config_text(config, "CONFIG_ARCH") != "arm":
        raise ValueError("Only Arm Cortex-M Zephyr builds are supported at this stage")
    image = path / project_contract.KERNEL_ELF
    return ZephyrProject(build, config, _elf_regions(image), image)


class ZephyrRtos:
    """Load final single-image Zephyr artifacts from a build-directory root."""

    name = NAME

    def project_elfs(self, project_path: Path) -> list[Path]:
        """Validate all required artifacts and ELF metadata before loading any symbols."""
        _read_project(project_path)
        return [project_path / project_contract.KERNEL_ELF]

    def load_project(self, project_path: Path, primary_elf: Path) -> ZephyrProject:
        """Cache the build context after its final image's symbols have been loaded."""
        if primary_elf != project_path / project_contract.KERNEL_ELF:
            raise ValueError("Expected zephyr/zephyr.elf as the primary Zephyr image")
        return _read_project(project_path)
