# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""CoreSight component identification and ROM-table discovery utilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .target_memory import TargetMemory, TargetReadError

MCU_ROM_TABLE_ADDRESS = 0xE00FE000
PROCESSOR_ROM_TABLE_ADDRESS = 0xE00FF000

ROM_TABLE_COMPONENT_CLASS = 0x1

_CIDR_PREAMBLE_MASK = 0xFFFF0FFF
_CIDR_PREAMBLE_VALUE = 0xB105000D
_CIDR_CLASS_MASK = 0x0000F000
_CIDR_CLASS_SHIFT = 12
_PIDR_OFFSETS = (0xFE0, 0xFE4, 0xFE8, 0xFEC, 0xFD0)
_CIDR_OFFSETS = (0xFF0, 0xFF4, 0xFF8, 0xFFC)


class CoreSightError(RuntimeError):
    """Base error for malformed or inaccessible CoreSight metadata."""


class ComponentIdentityError(CoreSightError):
    """A component's CIDR or PIDR registers are invalid or inaccessible."""


class RomTableError(CoreSightError):
    """A ROM table is malformed, inaccessible, or exceeds safety limits."""


@dataclass(frozen=True)
class Jep106Identity:
    """A complete JEP106 manufacturer identity."""

    bank: int
    code: int

    def __post_init__(self) -> None:
        """Validate architecturally sized JEP106 fields."""
        if not 0 <= self.bank <= 0xF:
            raise ValueError("JEP106 continuation bank must fit in four bits")
        if not 0 <= self.code <= 0x7F:
            raise ValueError("JEP106 identity code must fit in seven bits")

    def display(self) -> str:
        """Return a report-ready representation of the complete identity."""
        return f"bank {self.bank}, code 0x{self.code:02X}"


@dataclass(frozen=True)
class ComponentID:
    """Validated CoreSight Component ID register fields."""

    raw: tuple[int, int, int, int]
    component_class: int


@dataclass(frozen=True)
class PeripheralID:
    """Decoded CoreSight Peripheral ID register fields."""

    raw: tuple[int, int, int, int, int]
    part_number: int
    jedec_present: bool
    jep106: Jep106Identity | None


@dataclass(frozen=True)
class ComponentIdentity:
    """Validated component and peripheral identity at one 4-KiB base."""

    base: int
    component_id: ComponentID
    peripheral_id: PeripheralID


@dataclass(frozen=True)
class RomTableEntry:
    """One resolved format-1 ROM-table entry."""

    table_base: int
    index: int
    raw: int
    component_base: int


@dataclass(frozen=True)
class RomTable:
    """A validated ROM table and the components reached from it."""

    base: int
    identity: ComponentIdentity
    entries: tuple[RomTableEntry, ...]
    components: tuple[ComponentIdentity, ...]


@dataclass(frozen=True)
class RomTableDiscovery:
    """A best-effort ROM-table scan with an explicit available state."""

    base: int
    table: RomTable | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        """Ensure a discovery result is either available or unavailable."""
        if (self.table is None) == (self.unavailable_reason is None):
            raise ValueError("a ROM-table discovery must have exactly one state")

    @property
    def is_available(self) -> bool:
        """Whether a valid ROM table was discovered."""
        return self.table is not None


@dataclass(frozen=True)
class CoreSightDiscovery:
    """Best-effort MCU and processor ROM-table discovery results."""

    mcu_rom: RomTableDiscovery


def decode_component_id(raw: Sequence[int]) -> ComponentID:
    """Validate and decode standard Component ID register bytes.

    Parameters
    ----------
    raw
        CIDR0 through CIDR3, each represented by its low byte.

    Returns
    -------
    ComponentID
        The validated component class and original register bytes.

    Raises
    ------
    ComponentIdentityError
        If the bytes do not carry the standard preamble.
    """
    values = _register_bytes(raw, 4, "CIDR")
    packed = sum(value << (index * 8) for index, value in enumerate(values))
    if (packed & _CIDR_PREAMBLE_MASK) != _CIDR_PREAMBLE_VALUE:
        raise ComponentIdentityError(
            "CIDR does not contain the standard CoreSight component preamble"
        )
    return ComponentID(
        raw=(values[0], values[1], values[2], values[3]),
        component_class=(packed & _CIDR_CLASS_MASK) >> _CIDR_CLASS_SHIFT,
    )


def decode_peripheral_id(raw: Sequence[int]) -> PeripheralID:
    """Decode standard Peripheral ID register bytes.

    JEP106 fields are exposed only when PIDR2's JEDEC-present bit is set.  The
    seven-bit identity code combines PIDR1's high nibble with PIDR2's low
    three bits; PIDR4 supplies the continuation bank.

    Parameters
    ----------
    raw
        PIDR0 through PIDR4, each represented by its low byte.

    Returns
    -------
    PeripheralID
        The component part number and, when advertised, JEP106 identity.
    """
    values = _register_bytes(raw, 5, "PIDR")
    part_number = values[0] | ((values[1] & 0x0F) << 8)
    jedec_present = bool(values[2] & 0x08)
    jep106 = None
    if jedec_present:
        jep106 = Jep106Identity(
            bank=values[4] & 0x0F,
            code=((values[2] & 0x07) << 4) | ((values[1] >> 4) & 0x0F),
        )
    return PeripheralID(
        raw=(values[0], values[1], values[2], values[3], values[4]),
        part_number=part_number,
        jedec_present=jedec_present,
        jep106=jep106,
    )


def read_component_identity(
    reader: TargetMemory,
    base: int,
    *,
    expected_class: int | None = None,
) -> ComponentIdentity:
    """Read and validate one CoreSight component identity.

    Parameters
    ----------
    reader
        Target-memory reader used for register reads.
    base
        The 4-KiB-aligned component base.
    expected_class
        An optional required CoreSight component class.

    Returns
    -------
    ComponentIdentity
        The validated CIDR and decoded PIDR values.

    Raises
    ------
    ComponentIdentityError
        If the base, CIDR, PIDR, or expected component class is invalid.
    """
    _validate_component_base(base)
    try:
        component_id = decode_component_id(
            tuple(reader.read_uint32(base + offset) & 0xFF for offset in _CIDR_OFFSETS)
        )
        peripheral_id = decode_peripheral_id(
            tuple(reader.read_uint32(base + offset) & 0xFF for offset in _PIDR_OFFSETS)
        )
    except TargetReadError as error:
        raise ComponentIdentityError(
            f"could not read component identity at 0x{base:08X}: {error}"
        ) from error

    if expected_class is not None and component_id.component_class != expected_class:
        raise ComponentIdentityError(
            f"component at 0x{base:08X} has class 0x{component_id.component_class:X}; "
            f"expected 0x{expected_class:X}"
        )
    return ComponentIdentity(
        base=base,
        component_id=component_id,
        peripheral_id=peripheral_id,
    )


def walk_rom_table(
    reader: TargetMemory,
    table_base: int,
    *,
    max_entries: int = 256,
    max_nesting: int = 8,
) -> RomTable:
    """Traverse a bounded format-1 CoreSight ROM table.

    Parameters
    ----------
    reader
        Target-memory reader used for all table and identity reads.
    table_base
        The 4-KiB-aligned base address of the root ROM table.
    max_entries
        Maximum number of nonzero entries across the complete traversal.
    max_nesting
        Maximum number of nested ROM-table levels below the root.

    Returns
    -------
    RomTable
        The root table, resolved entries, and validated reachable components.

    Raises
    ------
    RomTableError
        If the table is malformed, inaccessible, cyclic, or exceeds a limit.
    """
    _validate_component_base(table_base)
    if max_entries <= 0:
        raise ValueError("max_entries must be positive")
    if max_nesting < 0:
        raise ValueError("max_nesting must not be negative")

    try:
        root_identity = read_component_identity(
            reader, table_base, expected_class=ROM_TABLE_COMPONENT_CLASS
        )
    except ComponentIdentityError as error:
        raise RomTableError(f"invalid ROM table at 0x{table_base:08X}: {error}") from error

    entries: list[RomTableEntry] = []
    components: list[ComponentIdentity] = []
    visited_tables: set[int] = set()
    visited_components: set[int] = {table_base}

    def scan_table(base: int, identity: ComponentIdentity, depth: int) -> None:
        """Read one table after its identity has already been validated."""
        if base in visited_tables:
            raise RomTableError(f"ROM-table cycle at 0x{base:08X}")
        visited_tables.add(base)

        for index in range(max_entries):
            try:
                raw = reader.read_uint32(base + index * 4)
            except TargetReadError as error:
                raise RomTableError(
                    f"could not read ROM-table entry {index} at 0x{base:08X}: {error}"
                ) from error
            if raw == 0:
                return
            if len(entries) >= max_entries:
                raise RomTableError(f"ROM table at 0x{base:08X} exceeded {max_entries} entries")

            component_base, is_present = _resolve_rom_entry(base, index, raw)
            if not is_present:
                continue
            if component_base in visited_components:
                raise RomTableError(
                    f"ROM table at 0x{base:08X} repeats component 0x{component_base:08X}"
                )

            try:
                component = read_component_identity(reader, component_base)
            except ComponentIdentityError as error:
                raise RomTableError(
                    f"invalid component in ROM table at 0x{base:08X}, entry {index}: {error}"
                ) from error

            visited_components.add(component_base)
            entries.append(
                RomTableEntry(
                    table_base=base,
                    index=index,
                    raw=raw,
                    component_base=component_base,
                )
            )
            components.append(component)
            if component.component_id.component_class == ROM_TABLE_COMPONENT_CLASS:
                if depth >= max_nesting:
                    raise RomTableError(
                        f"ROM table at 0x{base:08X} exceeded nesting limit {max_nesting}"
                    )
                scan_table(component_base, component, depth + 1)
        raise RomTableError(f"ROM table at 0x{base:08X} exceeded {max_entries} entries")

    scan_table(table_base, root_identity, 0)
    return RomTable(
        base=table_base,
        identity=root_identity,
        entries=tuple(entries),
        components=tuple(components),
    )


def discover_rom_tables(reader: TargetMemory) -> CoreSightDiscovery:
    """Best-effort scan the MCU root and processor CoreSight ROM tables.

    The MCU root is scanned independently from the processor ROM table so a
    diagnostic failure in either table never prevents reporting CPUID data.

    Parameters
    ----------
    reader
        Target-memory reader used for table discovery.

    Returns
    -------
    CoreSightDiscovery
        Available tables or explicit unavailability reasons for both roots.
    """
    rom_table = None
    for base_address in [MCU_ROM_TABLE_ADDRESS, PROCESSOR_ROM_TABLE_ADDRESS]:
        rom_table = _discover_rom_table(reader, base_address, require_jep106=True)
        if not rom_table.unavailable_reason:
            break
    return CoreSightDiscovery(
        mcu_rom=rom_table,
    )


def _discover_rom_table(
    reader: TargetMemory,
    base: int,
    *,
    require_jep106: bool = False,
) -> RomTableDiscovery:
    """Scan one fixed ROM-table root without making discovery command-fatal."""
    try:
        table = walk_rom_table(reader, base)
        if require_jep106 and table.identity.peripheral_id.jep106 is None:
            raise RomTableError(
                f"ROM table at 0x{base:08X} does not advertise a JEDEC manufacturer identity"
            )
    except RomTableError as error:
        return RomTableDiscovery(base=base, unavailable_reason=str(error))
    return RomTableDiscovery(base=base, table=table)


def _register_bytes(raw: Sequence[int], expected_count: int, name: str) -> tuple[int, ...]:
    """Validate a fixed-length sequence of low-byte register values."""
    if len(raw) != expected_count:
        raise ComponentIdentityError(f"{name} requires {expected_count} register bytes")
    if any(not isinstance(value, int) or not 0 <= value <= 0xFF for value in raw):
        raise ComponentIdentityError(f"{name} register values must be unsigned bytes")
    return tuple(raw)


def _validate_component_base(base: int) -> None:
    """Validate the address range and alignment shared by component accesses."""
    if not 0 <= base <= 0xFFFFF000:
        raise ComponentIdentityError("component base must be a 32-bit address")
    if base & 0xFFF:
        raise ComponentIdentityError("component base must be 4-KiB aligned")


def _resolve_rom_entry(table_base: int, index: int, raw: int) -> tuple[int, bool]:
    """Validate and resolve one nonzero format-1 ROM-table entry."""
    if not 0 <= raw <= 0xFFFFFFFF:
        raise RomTableError(f"ROM-table entry {index} is not an unsigned 32-bit value")
    is_present = raw & 0x01
    if not raw & 0x02:
        raise RomTableError(f"ROM-table entry {index} does not use format 1")

    offset = int.from_bytes((raw & 0xFFFFF000).to_bytes(length=4, signed=False), signed=True)

    component_base = table_base + offset
    if not 0 <= component_base <= 0xFFFFF000:
        raise RomTableError(f"ROM-table entry {index} resolves outside 32-bit address space")
    return component_base, is_present
