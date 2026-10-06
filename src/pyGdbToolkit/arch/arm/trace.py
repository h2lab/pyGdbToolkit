"""Read-only Arm trace capability probing through discovered CoreSight components."""

from __future__ import annotations

from ...target_memory import TargetMemory, TargetReadError
from ..base import RegisterValue, TargetDescription
from ..trace import TraceCapabilities, TraceComponent, TraceComponentKind
from .coresight import CoreSightDiscovery, Jep106Identity, discover_rom_tables
from .cortex_m import CortexMTargetDescription

_ARM = Jep106Identity(bank=4, code=0x3B)
_DEVARCH = 0xFBC
_DEVID = 0xFC8
_IDR = 0x1E4
_IDR3 = 0x1EC
_RAM_DEPTH = 0x004
_ARM_ARCH_MASK = 0xFFF0FFFF
_ETM_ARCH = 0x47704A13
_MTB_ARCH = 0x47700A31
_LEGACY_ETM_PARTS = frozenset((0x921, 0x924, 0x925, 0x926, 0x955, 0x956))
_ETM4_PARTS = frozenset((0x959, 0x95A, 0x95D, 0x95E, 0x975))


def probe_trace_capabilities(reader: TargetMemory, target: TargetDescription) -> TraceCapabilities:
    """Probe the Cortex-M ROM roots only for the currently supported Arm family."""
    if not isinstance(target, CortexMTargetDescription):
        return TraceCapabilities(
            unavailable_reason=f"trace probing unsupported for {target.family}"
        )
    return discover_trace_capabilities(reader)


def discover_trace_capabilities(
    reader: TargetMemory,
    discovery: CoreSightDiscovery | None = None,
) -> TraceCapabilities:
    """Inspect reachable trace components without powering, unlocking, or enabling them.

    DEVARCH identifies architected ETM/MTB implementations independently of PIDR.
    Legacy part-number fallbacks require the full Arm JEP106 identity. Optional
    register faults preserve component presence and the unavailable evidence.
    """
    if discovery is None:
        discovery = discover_rom_tables(reader)
    table = discovery.mcu_rom.table
    if table is None:
        return TraceCapabilities(unavailable_reason=discovery.mcu_rom.unavailable_reason)

    components: list[TraceComponent] = []
    for identity in table.components:
        if identity.component_id.component_class != 9:
            continue
        base = identity.base
        registers: list[RegisterValue] = []
        devarch = _read(reader, base, _DEVARCH, "DEVARCH", registers)
        arch_id = None if devarch is None else devarch & _ARM_ARCH_MASK
        arm_part = (
            identity.peripheral_id.part_number if identity.peripheral_id.jep106 == _ARM else None
        )
        if arch_id == _ETM_ARCH:
            assert devarch is not None
            components.append(_etm(reader, base, registers, devarch=devarch))
        elif arch_id == _MTB_ARCH:
            components.append(
                TraceComponent(TraceComponentKind.MTB, base, registers=tuple(registers))
            )
        elif devarch is not None and devarch & (1 << 20):
            continue
        elif arm_part in _LEGACY_ETM_PARTS or arm_part in _ETM4_PARTS:
            components.append(_etm(reader, base, registers, modern=arm_part in _ETM4_PARTS))
        elif arm_part in (0x932, 0x9A3):
            components.append(
                TraceComponent(TraceComponentKind.MTB, base, registers=tuple(registers))
            )
        elif arm_part == 0x907:
            depth = _read(reader, base, _RAM_DEPTH, "RAM_DEPTH", registers)
            components.append(
                TraceComponent(
                    TraceComponentKind.ETB,
                    base,
                    buffer_size_bytes=None if depth is None else depth * 4,
                    registers=tuple(registers),
                )
            )
        elif arm_part in (0x961, 0x9E8, 0x9E9, 0x9EA):
            devid = _read(reader, base, _DEVID, "DEVID", registers)
            if devid is None:
                continue
            configuration = (devid >> 6) & 0x3
            if configuration not in (0, 2):
                continue
            depth = _read(reader, base, _RAM_DEPTH, "RSZ", registers)
            components.append(
                TraceComponent(
                    TraceComponentKind.ETB if configuration == 0 else TraceComponentKind.ETF,
                    base,
                    buffer_size_bytes=None if depth is None else depth * 4,
                    registers=tuple(registers),
                )
            )
    return TraceCapabilities(components=tuple(components))


def _read(
    reader: TargetMemory,
    base: int,
    offset: int,
    name: str,
    registers: list[RegisterValue],
) -> int | None:
    """Retain optional register values and target faults as probe evidence."""
    address = base + offset
    try:
        value = reader.read_uint32(address)
    except TargetReadError as error:
        registers.append(RegisterValue.unavailable(name, address, 32, str(error)))
        return None
    registers.append(RegisterValue.known(name, address, 32, value))
    return value


def _etm(
    reader: TargetMemory,
    base: int,
    registers: list[RegisterValue],
    *,
    devarch: int | None = None,
    modern: bool = False,
) -> TraceComponent:
    """Decode ETM version and advertised security filtering, not authentication."""
    version = None
    security = None
    secure_levels = None
    nonsecure_levels = None
    if devarch is not None or modern:
        if devarch is not None:
            version = f"4.{(devarch >> 16) & 0xF}"
        else:
            idr = _read(reader, base, _IDR, "TRCIDR1", registers)
            if idr is not None and (idr >> 8) & 0xF == 4:
                version = f"4.{(idr >> 4) & 0xF}"
        idr3 = _read(reader, base, _IDR3, "TRCIDR3", registers)
        if idr3 is not None:
            secure_levels = (idr3 >> 16) & 0xF
            nonsecure_levels = (idr3 >> 20) & 0xF
            security = bool(secure_levels and nonsecure_levels)
    else:
        idr = _read(reader, base, _IDR, "ETMIDR", registers)
        if idr is not None:
            architecture = (idr >> 4) & 0xFF
            if 0x10 <= architecture <= 0x2F:
                version = f"{(architecture >> 4) + 1}.{architecture & 0xF}"
                security = bool(idr & (1 << 12))
    return TraceComponent(
        TraceComponentKind.ETM,
        base,
        version=version,
        security_filtering=security,
        secure_exception_levels=secure_levels,
        nonsecure_exception_levels=nonsecure_levels,
        registers=tuple(registers),
    )
