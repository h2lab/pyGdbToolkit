# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""GDB metadata adapters for portable memory-map discovery."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Callable
from xml.etree import ElementTree

import gdb

from .arch.arm.memmap import CortexMMemoryMapProvider
from .arch.arm.memmap_baselines import ARM_MEMORY_BASELINES
from .arch.memmap import (
    GenericMemoryMapProvider,
    MemoryMapRegistry,
    MemoryRegion,
    RuntimeMemoryEvidence,
    TargetFingerprint,
)
from .svd import SvdDevice
from .target_memory import TargetMemoryReader, TargetReadError

DEFAULT_MEMORY_MAP_REGISTRY = MemoryMapRegistry(
    (CortexMMemoryMapProvider(),), baselines=ARM_MEMORY_BASELINES
)


def parse_target_identity(output: str, architecture: str) -> TargetFingerprint | None:
    """Require a complete consistent pyOCD identity, still marked server-reported."""
    fields = dict(re.findall(r"^\s*(Target type|Vendor|Part number):\s*(.+?)\s*$", output, re.M))
    if not all(fields.get(name) for name in ("Target type", "Vendor", "Part number")):
        return None

    def normalize(value: str) -> str:
        return "".join(character for character in value.casefold() if character.isalnum())

    if normalize(fields["Target type"]) != normalize(fields["Part number"]):
        return None
    return TargetFingerprint(
        fields["Vendor"],
        fields["Part number"],
        architecture,
        "server-reported",
        ("pyOCD monitor show target (server configuration, not hardware confirmation)",),
        server_soc=fields["Part number"],
    )


@contextmanager
def gdb_memory_policy(bypass: bool = False) -> Iterator[None]:
    """Temporarily bypass the local GDB memory-map restriction, never target security."""
    if not bypass:
        yield
        return
    previous = bool(gdb.parameter("mem inaccessible-by-default"))
    try:
        gdb.execute("set mem inaccessible-by-default off", to_string=True)
        yield
    finally:
        gdb.execute(
            "set mem inaccessible-by-default " + ("on" if previous else "off"), to_string=True
        )


def gdb_target_fingerprint(
    architecture: str,
    dap: dict[str, Any] | None,
    registry: MemoryMapRegistry = DEFAULT_MEMORY_MAP_REGISTRY,
) -> TargetFingerprint | None:
    """Combine backend identity with architecture-owned read-only hardware evidence."""
    server = None
    if dap is not None and dap.get("backend") == "PyOcdMonitorTransport":
        try:
            server = parse_target_identity(
                gdb.execute("monitor show target", to_string=True), architecture
            )
        except gdb.error:
            pass
    provider = registry.provider(architecture)
    if isinstance(provider, GenericMemoryMapProvider):
        return server
    try:
        require_stopped_target()
        with gdb_memory_policy(True):
            return provider.fingerprint(TargetMemoryReader(), architecture, server)
    except (gdb.error, TargetReadError, ValueError):
        return server


def gdb_execution_evidence(
    architecture: str,
    fingerprint: TargetFingerprint | None,
    regions: list[MemoryRegion],
    registry: MemoryMapRegistry = DEFAULT_MEMORY_MAP_REGISTRY,
) -> RuntimeMemoryEvidence:
    """Read the current stopped frame and delegate optional memory reads to its provider."""
    result = RuntimeMemoryEvidence()
    try:
        require_stopped_target()
        frame = gdb.selected_frame()
    except (gdb.error, AttributeError) as error:
        result.notes.append(f"Execution-register discovery unavailable: {error}")
        return result
    registers: dict[str, int] = {}
    for name in ("pc", "sp"):
        try:
            registers[name] = int(frame.read_register(name))
        except (gdb.error, ValueError, TypeError):
            result.notes.append(f"Execution register {name.upper()} unavailable")
    provider = registry.provider(architecture)
    if isinstance(provider, GenericMemoryMapProvider):
        evidence = provider.runtime_evidence(TargetMemoryReader(), registers, fingerprint, regions)
        evidence.notes.extend(result.notes)
        return evidence
    try:
        with gdb_memory_policy(True):
            evidence = provider.runtime_evidence(
                TargetMemoryReader(), registers, fingerprint, regions
            )
        evidence.notes.extend(result.notes)
        return evidence
    except (gdb.error, TargetReadError, ValueError) as error:
        result = GenericMemoryMapProvider().runtime_evidence(
            TargetMemoryReader(), registers, fingerprint, regions
        )
        result.notes.append(f"Architectural execution evidence unavailable: {error}")
        return result


def parse_server_map(xml: str) -> list[MemoryRegion]:
    """Parse a bounded remote memory-map document as declared metadata."""
    if len(xml) > 1 << 20 or re.search(r"<!\s*ENTITY|<!\s*DOCTYPE[^>]*\[", xml, re.I):
        raise ValueError("remote memory map is too large or contains entity definitions")
    try:
        root = ElementTree.fromstring(xml)
        if root.tag != "memory-map":
            raise ValueError("expected a memory-map root")
        regions = []
        for element in root.findall("memory"):
            start = int(element.attrib["start"], 0)
            length = int(element.attrib["length"], 0)
            kind = {"ram": "ram", "rom": "rom", "flash": "flash"}.get(
                element.attrib["type"], "unknown"
            )
            regions.append(
                MemoryRegion(
                    start,
                    start + length,
                    kind,
                    "gdb-server",
                    name=element.attrib.get(
                        "name", element.findtext("./property[@name='name']", "")
                    ),
                    probe_allowed=kind in ("ram", "rom", "flash"),
                    evidence="Declared by server; physical extent and access not verified",
                )
            )
        return regions
    except (ElementTree.ParseError, KeyError) as error:
        raise ValueError(f"invalid remote memory map: {error}") from error


def _packet_bytes(display: str) -> bytes:
    """Decode GDB hex display escapes followed by RSP binary escapes."""
    raw = re.sub(r"\\x([0-9a-fA-F]{2})", lambda match: chr(int(match[1], 16)), display)
    encoded = raw.encode("latin-1")
    result = bytearray()
    index = 0
    while index < len(encoded):
        value = encoded[index]
        if value == 0x7D:
            index += 1
            if index == len(encoded):
                raise ValueError("incomplete RSP binary escape in memory map")
            value = encoded[index] ^ 0x20
        result.append(value)
        index += 1
    return bytes(result)


def remote_memory_map(execute: Callable[[str], str]) -> list[MemoryRegion]:
    """Read qXfer metadata with bounded chunks, without reading target memory."""
    chunks: list[bytes] = []
    offset = 0
    for _ in range(256):
        output = execute(f"maintenance packet qXfer:memory-map:read::{offset:x},1000")
        match = re.search(r'^\s*received:\s*"(.*)"\s*$', output, re.M | re.I)
        if match is None or not match[1] or match[1][0] not in ("m", "l"):
            raise ValueError("server does not expose qXfer memory-map metadata")
        response = match[1]
        chunk = _packet_bytes(response[1:])
        if not chunk and response[0] == "m":
            raise ValueError("server returned an empty continuing memory-map chunk")
        offset += len(chunk)
        if offset > 1 << 20:
            raise ValueError("remote memory map exceeds 1 MiB")
        chunks.append(chunk)
        if response[0] == "l":
            return parse_server_map(b"".join(chunks).decode("utf-8"))
    raise ValueError("remote memory map exceeds 256 packets")


def elf_regions(output: str) -> list[MemoryRegion]:
    """Parse GDB's section inventory; ALLOC sections are placement hints only."""
    regions = []
    for match in re.finditer(
        r"^\s*(?:\[\s*\d+\]\s*)?(0x[\da-f]+)->(0x[\da-f]+)" r"\s+at\s+0x[\da-f]+:\s+(\S+)([^\n]*)$",
        output,
        re.M | re.I,
    ):
        if "ALLOC" not in match[4].split():
            continue
        start, end = int(match[1], 16), int(match[2], 16)
        if start < end:
            regions.append(
                MemoryRegion(
                    start,
                    end,
                    "unknown",
                    "elf",
                    match[3],
                    evidence="Allocated section; not a physical memory or permissions description",
                )
            )
    return regions


def svd_regions(device: SvdDevice) -> list[MemoryRegion]:
    """Describe register envelopes without claiming gaps or peripheral sizes."""
    if device.address_unit_bits != 8:
        raise ValueError("SVD discovery requires 8-bit address units")
    regions = []
    for peripheral in device.peripherals:
        if not peripheral.registers:
            continue
        start = peripheral.base_address + min(
            register.address_offset for register in peripheral.registers
        )
        end = peripheral.base_address + max(
            register.address_offset + max(1, (register.size + 7) // 8)
            for register in peripheral.registers
        )
        regions.append(
            MemoryRegion(
                start,
                end,
                "registers",
                "svd",
                peripheral.name,
                evidence="Envelope of described registers; gaps and physical extent unknown",
            )
        )
    return regions


def gdb_memory_context() -> dict[str, Any]:
    """Capture access identity so samples cannot silently move to another core."""
    inferior = gdb.selected_inferior()
    if not inferior.is_valid() or not inferior.threads():
        raise gdb.GdbError("Connect to a target before using memmap")
    thread = gdb.selected_thread()
    if thread is None:
        raise gdb.GdbError("Select a target thread before using memmap")
    connection = inferior.connection
    return {
        "view": "selected-gdb-inferior",
        "inferior": inferior.num,
        "pid": inferior.pid,
        "connection": None if connection is None else connection.num,
        "endpoint": None if connection is None else connection.details,
        "thread": thread.global_num,
        "thread_name": thread.name,
        "architecture": inferior.architecture().name(),
        "objfiles": [objfile.filename for objfile in gdb.objfiles()],
    }


def require_stopped_target() -> None:
    """Refuse sampling while any thread in the selected inferior is running."""
    threads = gdb.selected_inferior().threads()
    if not threads or any(not thread.is_stopped() for thread in threads):
        raise gdb.GdbError("Stop all threads of the selected inferior before probing memory")
