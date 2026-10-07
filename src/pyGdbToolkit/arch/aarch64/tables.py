# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Bounded classic AArch64 stage-1 table inspection through explicit physical access."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ...target_memory import PhysicalTableMemory, TargetReadError
from ..diagnostics import DiagnosticFinding, DiagnosticSeverity
from .cpu import physical_address_bits
from .features import FeatureReport, FeatureSupport
from .isolation import IsolationReport

_ADDRESS_MASK = ((1 << 48) - 1) & ~0xFFF


@dataclass(frozen=True)
class TableWalkLimits:
    """Hard caps on total reads, recursion, table visits and retained mappings."""

    descriptors: int = 4096
    tables: int = 64
    mappings: int = 1024

    def __post_init__(self) -> None:
        """Keep caller-selected budgets bounded even for corrupt tables."""
        if not (
            1 <= self.descriptors <= 65536
            and 1 <= self.tables <= 256
            and 1 <= self.mappings <= 8192
        ):
            raise ValueError("table walk limits exceed supported bounds")


@dataclass(frozen=True)
class TableMapping:
    """Permissions derived from one leaf and its inherited table restrictions."""

    virtual_address: int
    physical_address: int
    size: int
    descriptor_address: int
    privileged_writable: bool
    privileged_executable: bool
    user_writable: bool
    user_executable: bool
    dirty_write_possible: bool


@dataclass(frozen=True)
class TableWalkReport:
    """A bounded partial inventory, never proof of the target's effective mappings."""

    mappings: tuple[TableMapping, ...] = ()
    descriptors_read: int = 0
    tables_visited: int = 0
    complete: bool = False
    reason: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class _Root:
    address: int
    virtual_address: int
    level: int
    entries: int
    hpd: bool


@dataclass(frozen=True)
class _Restrictions:
    readonly: bool = False
    no_user: bool = False
    pxn: bool = False
    uxn: bool = False


class _WalkStopped(Exception):
    """Abort bounded traversal while retaining partial evidence."""

    pass


def _prepare(
    report: IsolationReport, features: FeatureReport
) -> tuple[tuple[_Root, ...], int, Literal["little", "big"], bool, bool]:
    regime = report.mmu.regime
    if regime is None:
        raise _WalkStopped("translation regime is unavailable")
    suffix = regime.suffix
    sctlr, tcr = f"SCTLR_{suffix}", f"TCR_{suffix}"
    if report.bits(sctlr, 0) != 1:
        raise _WalkStopped("stage-1 MMU is not observed enabled")
    wxn, endian = report.bits(sctlr, 19), report.bits(sctlr, 25)
    if wxn is None or endian is None:
        raise _WalkStopped("WXN or table endianness is unavailable")
    ids = IsolationReport(report.context, report.mmu, features.registers)
    for shift in (0, 8, 16, 32):
        if report.bits("ID_AA64MMFR3_EL1", shift, 4) != 0:
            raise _WalkStopped(
                "TCR2, indirect/overlay permissions or D128 support is present/unknown"
            )
    if report.bits(tcr, 59 if regime.dual_range else 32) != 0:
        raise _WalkStopped("DS extended descriptor format is enabled/unknown")
    ps = report.bits(tcr, 32 if regime.dual_range else 16, 3)
    parange = ids.bits("ID_AA64MMFR0_EL1", 0, 4)
    pa_bits = None if ps is None else physical_address_bits(ps)
    maximum = None if parange is None else physical_address_bits(parange)
    if pa_bits is None or pa_bits > 48 or maximum is None or pa_bits > maximum:
        raise _WalkStopped("physical output size is extended, unavailable or inconsistent")
    if ids.bits("ID_AA64MMFR0_EL1", 28, 4) not in (0, 1):
        raise _WalkStopped("4 KiB translation granule is unsupported/unknown")
    hafdbs = features.capability("HAFDBS")
    ha = report.bits(tcr, 39 if regime.dual_range else 21)
    hd = report.bits(tcr, 40 if regime.dual_range else 22)
    if ha is None or hd is None or hafdbs.support is FeatureSupport.UNKNOWN:
        raise _WalkStopped("hardware dirty/access update semantics are unknown")
    if hafdbs.support is FeatureSupport.ABSENT and (ha or hd):
        raise _WalkStopped("reserved HA/HD controls are set without HAFDBS")
    if hd and hafdbs.encoding == 1:
        raise _WalkStopped("HD is set without hardware dirty update support")
    dirty = bool(ha and hd and hafdbs.encoding is not None and hafdbs.encoding >= 2)
    roots: list[_Root] = []
    hpds = features.capability("HPDS").support
    for index in range(2 if regime.dual_range else 1):
        if regime.dual_range:
            epd = report.bits(tcr, 23 if index else 7)
            if epd != 0:
                raise _WalkStopped(
                    "disabled/unknown table walks leave cached translations unqualified"
                )
        tg = report.bits(tcr, 30 if index else 14, 2)
        if tg != (2 if index else 0):
            raise _WalkStopped("only classic 4 KiB granules are supported")
        size_offset = report.bits(tcr, 16 * index, 6)
        if size_offset is None or not 16 <= size_offset <= 39:
            raise _WalkStopped("only 25..48-bit classic virtual ranges are supported")
        va_bits = 64 - size_offset
        levels = (va_bits - 12 + 8) // 9
        level = 4 - levels
        entries = 1 << (va_bits - (12 + 9 * (3 - level)))
        ttbr = report.bits(f"TTBR{index}_{suffix}", 0, 64)
        if ttbr is None:
            raise _WalkStopped("translation table root register is unavailable")
        address = ttbr & ((1 << 48) - 1) & ~1
        if address % (entries * 8) or address + entries * 8 > 1 << pa_bits:
            raise _WalkStopped("root table alignment or physical range is invalid")
        hpd = report.bits(tcr, (42 if index else 41) if regime.dual_range else 24)
        if hpd is None or hpds is FeatureSupport.UNKNOWN:
            raise _WalkStopped("hierarchical permission semantics are unknown")
        if hpd and hpds is FeatureSupport.ABSENT:
            raise _WalkStopped("reserved HPD control is set without HPDS")
        roots.append(
            _Root(address, (1 << 64) - (1 << va_bits) if index else 0, level, entries, bool(hpd))
        )
    byteorder: Literal["little", "big"] = "big" if endian else "little"
    return tuple(roots), pa_bits, byteorder, bool(wxn), dirty


def walk_tables(
    physical: PhysicalTableMemory | None,
    report: IsolationReport,
    features: FeatureReport,
    limits: TableWalkLimits = TableWalkLimits(),
) -> TableWalkReport:
    """Walk only authorized physical table bytes, with no virtual-memory fallback."""
    if physical is None:
        return TableWalkReport(reason="no explicitly configured physical table-memory access")
    mappings: list[TableMapping] = []
    reads = 0
    tables = 0
    reason: str | None = None
    try:
        if not physical.stage1_addresses_are_physical or not physical.snapshot_is_valid():
            raise _WalkStopped(
                "physical address domain, halted context or coherence is not verified"
            )
        roots, pa_bits, byteorder, wxn, dirty = _prepare(report, features)
        assert report.mmu.regime is not None
        dual = report.mmu.regime.dual_range

        def visit(root: _Root, restrictions: _Restrictions, ancestors: tuple[int, ...]) -> None:
            nonlocal reads, tables
            if root.address in ancestors:
                raise _WalkStopped("cycle in translation table ancestry")
            if tables >= limits.tables:
                raise _WalkStopped("table visit budget reached")
            tables += 1
            span = 1 << (12 + 9 * (3 - root.level))
            for index in range(root.entries):
                if reads >= limits.descriptors:
                    raise _WalkStopped("descriptor read budget reached")
                address = root.address + 8 * index
                if not physical.snapshot_is_valid():
                    raise _WalkStopped("physical snapshot became stale")
                if not any(
                    region.start <= address and address + 8 <= region.end
                    for region in physical.table_ranges
                ):
                    raise _WalkStopped(
                        f"table descriptor at 0x{address:X} is outside authorized physical ranges"
                    )
                reads += 1
                data = physical.read_physical_bytes(address, 8)
                if len(data) != 8:
                    raise _WalkStopped(f"short descriptor read at 0x{address:X}")
                descriptor = int.from_bytes(data, byteorder)
                if not descriptor & 1:
                    continue
                kind = descriptor & 3
                virtual = root.virtual_address + index * span
                output = descriptor & _ADDRESS_MASK
                if root.level < 3 and kind == 3:
                    if descriptor & (1 << 63):
                        raise _WalkStopped("NSTable physical-domain transition is not supported")
                    if descriptor & (0xF << 48) or output + 4096 > 1 << pa_bits:
                        raise _WalkStopped("invalid next-table physical address")
                    inherited = (
                        restrictions
                        if root.hpd
                        else _Restrictions(
                            restrictions.readonly or bool(descriptor & (1 << 62)),
                            restrictions.no_user or (dual and bool(descriptor & (1 << 61))),
                            restrictions.pxn or (dual and bool(descriptor & (1 << 59))),
                            restrictions.uxn or bool(descriptor & (1 << 60)),
                        )
                    )
                    visit(
                        _Root(output, virtual, root.level + 1, 512, root.hpd),
                        inherited,
                        (*ancestors, root.address),
                    )
                    continue
                if root.level == 0 or (root.level == 3 and kind != 3) or descriptor & (3 << 48):
                    raise _WalkStopped("invalid or extended leaf descriptor")
                if output % span or output + span > 1 << pa_bits:
                    raise _WalkStopped("leaf output alignment or physical range is invalid")
                if len(mappings) >= limits.mappings:
                    raise _WalkStopped("mapping retention budget reached")
                potential_dirty = (
                    dirty and bool(descriptor & (1 << 51)) and bool(descriptor & (1 << 7))
                )
                writable = not restrictions.readonly and (
                    not descriptor & (1 << 7) or potential_dirty
                )
                user = dual and bool(descriptor & (1 << 6)) and not restrictions.no_user
                pxn = restrictions.pxn or (
                    bool(descriptor & (1 << 53)) if dual else bool(descriptor & (1 << 54))
                )
                uxn = restrictions.uxn or bool(descriptor & (1 << 54))
                if not dual:
                    pxn = pxn or restrictions.uxn
                mappings.append(
                    TableMapping(
                        virtual,
                        output,
                        span,
                        address,
                        writable,
                        not pxn and not (wxn and writable),
                        bool(user and writable),
                        bool(user and not uxn and not (wxn and writable)),
                        bool(potential_dirty and not restrictions.readonly),
                    )
                )

        for root in roots:
            visit(root, _Restrictions(), ())
        if not physical.snapshot_is_valid():
            raise _WalkStopped("physical snapshot became stale at completion")
    except (_WalkStopped, TargetReadError) as error:
        reason = str(error)
    return TableWalkReport(tuple(mappings), reads, tables, reason is None, reason, physical.source)


def audit_table_walk(report: TableWalkReport) -> tuple[DiagnosticFinding, ...]:
    """Expose traversal coverage and stage-1 W^X candidates without global PASS claims."""
    findings = [
        DiagnosticFinding(
            "MMU tables",
            DiagnosticSeverity.INFO,
            "Physical table traversal",
            f"Source: {report.source or 'unavailable'}; descriptors attempted={report.descriptors_read}; "
            f"tables visited={report.tables_visited}; mappings={len(report.mappings)}; "
            f"coverage={'complete within supported table roots' if report.complete else 'incomplete/unavailable'}. "
            f"{report.reason or 'All supported root entries were visited.'} "
            "Only classic 4 KiB stage-1 tables are supported. Other CPUs, TLBs, stage 2, "
            "aliases across mappings and effective security-state overrides are not certified.",
        )
    ]
    for mapping in report.mappings:
        levels = []
        if mapping.privileged_writable and mapping.privileged_executable:
            levels.append("privileged")
        if mapping.user_writable and mapping.user_executable:
            levels.append("EL0")
        if levels:
            findings.append(
                DiagnosticFinding(
                    "MMU tables",
                    (
                        DiagnosticSeverity.WARNING
                        if mapping.dirty_write_possible
                        else DiagnosticSeverity.ERROR
                    ),
                    "Stage-1 W^X candidate",
                    f"VA 0x{mapping.virtual_address:X}-0x{mapping.virtual_address + mapping.size:X} "
                    f"-> PA 0x{mapping.physical_address:X}; descriptor 0x{mapping.descriptor_address:X}; "
                    f"writable and executable for {', '.join(levels)} under the decoded stage-1 controls. "
                    f"{'DBM hardware dirty updates can grant write permission. ' if mapping.dirty_write_possible else ''}"
                    "Stored table permissions only: access-flag state, cached TLB permissions "
                    "and higher-level restrictions can differ from this candidate.",
                )
            )
    return tuple(findings)
