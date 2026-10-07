# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for authorized physical AArch64 table walks and bounded W^X checks."""

from __future__ import annotations

from dataclasses import replace

import pytest

from pyGdbToolkit.arch.aarch64.cpu import CpuRegister
from pyGdbToolkit.arch.aarch64.features import FeatureReport, decode_feature_report
from pyGdbToolkit.arch.aarch64.isolation import IsolationReport
from pyGdbToolkit.arch.aarch64.mmu import ExecutionContext, MmuReport, TranslationRegime
from pyGdbToolkit.arch.aarch64.tables import TableWalkLimits, audit_table_walk, walk_tables
from pyGdbToolkit.target_memory import (
    PhysicalMemoryRange,
    RestrictedPhysicalTableMemory,
    TargetReadError,
)


class Physical:
    """Provide sparse physical descriptors; reject all reads of mapped data."""

    source = "verified fixture physical backend"
    table_ranges = (PhysicalMemoryRange(0x1000, 0x5000),)
    stage1_addresses_are_physical = True

    def __init__(self, words: dict[int, int], byteorder: str = "little") -> None:
        """Initialize table words and an exact descriptor read log."""
        self.words = words
        self.byteorder = byteorder
        self.calls: list[int] = []
        self.valid = True

    def snapshot_is_valid(self) -> bool:
        """Represent a stopped, coherent selected CPU."""
        return self.valid

    def read_physical_bytes(self, address: int, size: int) -> bytes:
        """Return only eight-byte descriptors from authorized table RAM."""
        assert size == 8
        self.calls.append(address)
        return self.words.get(address, 0).to_bytes(8, self.byteorder)


def evidence(
    changes: dict[str, int] | None = None, regime: TranslationRegime = TranslationRegime.EL1
) -> tuple[IsolationReport, FeatureReport]:
    """Use a 25-bit VA range, classic direct permissions and verified granules."""
    tcr = 39 | ((39 << 16) | (2 << 30) if regime.dual_range else 0)
    values = {
        f"SCTLR_{regime.suffix}": 1,
        f"TCR_{regime.suffix}": tcr,
        f"TTBR0_{regime.suffix}": 0x1000,
        f"TTBR1_{regime.suffix}": 0x2000,
        "ID_AA64MMFR3_EL1": 0,
    }
    values.update(changes or {})
    registers = tuple(CpuRegister(name, 64, value) for name, value in values.items())
    level = 1 if regime is TranslationRegime.EL1 else 3 if regime is TranslationRegime.EL3 else 2
    report = IsolationReport(
        ExecutionContext(level, "test"), MmuReport(regime, registers), registers
    )
    features = decode_feature_report(
        (CpuRegister("ID_AA64MMFR0_EL1", 64, 0), CpuRegister("ID_AA64MMFR1_EL1", 64, 0))
    )
    return report, features


def test_complete_block_and_upper_range_walk() -> None:
    """Both roots cover distinct VA ranges and only table bytes are read."""
    report, features = evidence()
    physical = Physical({0x1000: 0x400000 | 1, 0x2000: 0x800000 | 1 | (1 << 7)})
    result = walk_tables(physical, report, features)
    assert result.complete
    assert result.descriptors_read == 32
    assert len(result.mappings) == 2
    assert result.mappings[0].size == 2 << 20
    assert result.mappings[1].virtual_address == (1 << 64) - (1 << 25)
    assert len(audit_table_walk(result)) == 2
    assert all(0x1000 <= address < 0x2080 for address in physical.calls)


@pytest.mark.parametrize("restriction", (1 << 62, 1 << 59))
def test_hierarchical_readonly_or_pxn_removes_wx(restriction: int) -> None:
    """A parent restriction applies to every descendant page."""
    report, features = evidence()
    result = walk_tables(
        Physical({0x1000: 0x3000 | 3 | restriction, 0x3000: 0x900000 | 3}), report, features
    )
    assert result.complete
    assert len(result.mappings) == 1
    assert len(audit_table_walk(result)) == 1


@pytest.mark.parametrize(
    "ap,pxn,uxn,pwx,uwx",
    (
        (0, 0, 0, True, False),
        (1, 1, 0, False, True),
        (1, 0, 1, True, False),
        (2, 0, 0, False, False),
        (3, 0, 0, False, False),
    ),
)
def test_leaf_permissions_per_exception_level(
    ap: int, pxn: int, uxn: int, pwx: bool, uwx: bool
) -> None:
    """Privileged and EL0 W^X are evaluated separately from AP/PXN/UXN."""
    report, features = evidence()
    leaf = 0x900000 | 3 | (ap << 6) | (pxn << 53) | (uxn << 54)
    result = walk_tables(Physical({0x1000: 0x3000 | 3, 0x3000: leaf}), report, features)
    mapping = result.mappings[0]
    assert (mapping.privileged_writable and mapping.privileged_executable) == pwx
    assert (mapping.user_writable and mapping.user_executable) == uwx


def test_wxn_and_hpd() -> None:
    """WXN is applied, while HPD explicitly disables parent permissions."""
    report, features = evidence({"SCTLR_EL1": 1 | (1 << 19)})
    physical = Physical({0x1000: 0x3000 | 3, 0x3000: 0x900000 | 3 | (1 << 6)})
    result = walk_tables(physical, report, features)
    assert not result.mappings[0].privileged_executable
    assert not result.mappings[0].user_executable
    report, features = evidence({"TCR_EL1": 39 | (39 << 16) | (2 << 30) | (1 << 41)})
    features = decode_feature_report(
        (CpuRegister("ID_AA64MMFR0_EL1", 64, 0), CpuRegister("ID_AA64MMFR1_EL1", 64, 1 << 12))
    )
    result = walk_tables(
        Physical({0x1000: 0x3000 | 3 | (1 << 62), 0x3000: 0x900000 | 3}), report, features
    )
    assert result.mappings[0].privileged_writable


@pytest.mark.parametrize(
    "limits,reason",
    (
        (TableWalkLimits(descriptors=1), "descriptor"),
        (TableWalkLimits(tables=1), "table visit"),
        (TableWalkLimits(mappings=1), "mapping retention"),
    ),
)
def test_budgets_stop_without_overrun(limits: TableWalkLimits, reason: str) -> None:
    """Corrupt or large tables cannot exceed configured read and output budgets."""
    report, features = evidence()
    physical = Physical({0x1000: 0x3000 | 3, 0x3000: 0x900000 | 3, 0x3008: 0x901000 | 3})
    result = walk_tables(physical, report, features, limits)
    assert not result.complete
    assert reason in result.reason
    assert result.descriptors_read <= limits.descriptors
    assert result.tables_visited <= limits.tables
    assert len(result.mappings) <= limits.mappings


def test_cycle_and_unauthorized_tables_stop() -> None:
    """Table pointers cannot escape approved ranges or revisit an ancestor."""
    report, features = evidence({"TCR_EL1": 16 | (16 << 16) | (2 << 30)})
    physical = Physical({0x1000: 0x1000 | 3})
    result = walk_tables(physical, report, features)
    assert "cycle" in result.reason
    physical = Physical({0x1000: 0xF000 | 3})
    result = walk_tables(physical, report, features)
    assert "outside authorized" in result.reason
    assert physical.calls == [0x1000]


@pytest.mark.parametrize(
    "change",
    (
        {"SCTLR_EL1": 0},
        {"ID_AA64MMFR3_EL1": 1},
        {"ID_AA64MMFR3_EL1": 1 << 32},
        {"TCR_EL1": 39 | (39 << 16) | (2 << 30) | (1 << 59)},
        {"TCR_EL1": 39 | (39 << 16) | (2 << 30) | (1 << 7)},
        {"TTBR0_EL1": 0x1002},
    ),
)
def test_unsupported_context_does_not_read_physical_memory(change: dict[str, int]) -> None:
    """Format and geometry must be established before the first physical read."""
    report, features = evidence(change)
    physical = Physical({})
    result = walk_tables(physical, report, features)
    assert not result.complete
    assert physical.calls == []


def test_unverified_physical_snapshot_and_missing_backend() -> None:
    """A virtual reader is never used in place of explicitly qualified physical access."""
    report, features = evidence()
    assert "no explicitly" in walk_tables(None, report, features).reason
    physical = Physical({})
    physical.valid = False
    assert not walk_tables(physical, report, features).complete
    assert physical.calls == []
    physical.valid = True
    physical.stage1_addresses_are_physical = False
    assert not walk_tables(physical, report, features).complete
    assert physical.calls == []


def test_physical_read_errors_and_short_reads_are_incomplete() -> None:
    """Access failures retain partial coverage, never a clean W^X verdict."""
    report, features = evidence()

    def read(address: int, size: int) -> bytes:
        raise TargetReadError(address, size, "denied")

    backend = RestrictedPhysicalTableMemory(
        "test", (PhysicalMemoryRange(0x1000, 0x5000),), read, lambda: True, True
    )
    assert "denied" in walk_tables(backend, report, features).reason
    backend = replace(backend, read=lambda address, size: b"short")
    assert "short" in walk_tables(backend, report, features).reason


@pytest.mark.parametrize("regime", (TranslationRegime.EL2, TranslationRegime.EL3))
def test_single_range_xn_and_big_endian(regime: TranslationRegime) -> None:
    """EL2/EL3 use XN and one root; SCTLR.EE governs descriptor byte order."""
    report, features = evidence({f"SCTLR_{regime.suffix}": 1 | (1 << 25)}, regime)
    result = walk_tables(Physical({0x1000: 0x400000 | 1 | (1 << 54)}, "big"), report, features)
    assert result.complete
    assert not result.mappings[0].privileged_executable
    assert not result.mappings[0].user_executable


def test_physical_adapter_authorization_and_context() -> None:
    """A concrete callback adapter enforces authorization independently of the walker."""
    calls = []

    def read(address: int, size: int) -> bytes:
        calls.append(address)
        return bytes(size)

    backend = RestrictedPhysicalTableMemory(
        "test", (PhysicalMemoryRange(0x1000, 0x1008),), read, lambda: True, True
    )
    assert backend.read_physical_bytes(0x1000, 8) == bytes(8)
    with pytest.raises(TargetReadError):
        backend.read_physical_bytes(0x1008, 8)
    with pytest.raises(TargetReadError):
        replace(backend, valid_context=lambda: False).read_physical_bytes(0x1000, 8)
    assert calls == [0x1000]


def test_context_becomes_stale_between_descriptors() -> None:
    """A context change stops the walk before the next physical read."""
    report, features = evidence()
    physical = Physical({})

    def read(address: int, size: int) -> bytes:
        physical.calls.append(address)
        physical.valid = False
        return bytes(size)

    backend = RestrictedPhysicalTableMemory(
        physical.source, physical.table_ranges, read, physical.snapshot_is_valid, True
    )
    result = walk_tables(backend, report, features)
    assert not result.complete
    assert "stale" in result.reason
    assert physical.calls == [0x1000]


def test_dbm_potential_write_is_not_a_clean_readonly_mapping() -> None:
    """HA/HD plus DBM can turn AP read-only into potential write permission."""
    report, features = evidence({"TCR_EL1": 39 | (39 << 16) | (2 << 30) | (1 << 39) | (1 << 40)})
    features = decode_feature_report(
        (CpuRegister("ID_AA64MMFR0_EL1", 64, 0), CpuRegister("ID_AA64MMFR1_EL1", 64, 2))
    )
    result = walk_tables(Physical({0x1000: 0x400000 | 1 | (1 << 7) | (1 << 51)}), report, features)
    assert result.complete
    assert result.mappings[0].dirty_write_possible
    finding = audit_table_walk(result)[1]
    assert finding.severity.value == "warning"
    assert "DBM" in finding.detail


def test_shared_subtable_is_visited_for_each_virtual_alias() -> None:
    """Shared tables are not confused with ancestry cycles or silently deduplicated."""
    report, features = evidence()
    result = walk_tables(
        Physical({0x1000: 0x3000 | 3, 0x1008: 0x3000 | 3, 0x3000: 0x900000 | 3}), report, features
    )
    assert result.complete
    assert [mapping.virtual_address for mapping in result.mappings] == [0, 2 << 20]


def test_no_user_parent_restriction_removes_el0_only() -> None:
    """APTable[0] denies user access without inventing a privileged restriction."""
    report, features = evidence()
    result = walk_tables(
        Physical({0x1000: 0x3000 | 3 | (1 << 61), 0x3000: 0x900000 | 3 | (1 << 6)}),
        report,
        features,
    )
    assert result.mappings[0].privileged_writable
    assert not result.mappings[0].user_writable
    assert not result.mappings[0].user_executable


@pytest.mark.parametrize("descriptor", (0x400001, 0x400000 | 3 | (1 << 48)))
def test_invalid_level_zero_leaf_rejected(descriptor: int) -> None:
    """A block at level zero or an extended table address is never decoded as a mapping."""
    report, features = evidence({"TCR_EL1": 16 | (16 << 16) | (2 << 30)})
    result = walk_tables(Physical({0x1000: descriptor}), report, features)
    assert not result.complete
    assert not result.mappings


@pytest.mark.parametrize("limit", (0, -1, 65537))
def test_read_budget_validation(limit: int) -> None:
    """The caller cannot remove hard read limits."""
    with pytest.raises(ValueError):
        TableWalkLimits(descriptors=limit)


def test_security_domain_transition_is_not_followed() -> None:
    """A security-domain transition cannot silently redirect the physical backend."""
    report, features = evidence()
    physical = Physical({0x1000: 0x3000 | 3 | (1 << 63)})
    result = walk_tables(physical, report, features)
    assert "physical-domain transition" in result.reason
    assert physical.calls == [0x1000]


def test_unknown_modern_permissions_prevent_all_table_reads() -> None:
    """Unreadable MMFR3 does not become absence of permission indirection."""
    report, features = evidence()
    report = replace(
        report,
        registers=tuple(
            register for register in report.registers if register.name != "ID_AA64MMFR3_EL1"
        ),
    )
    physical = Physical({})
    assert not walk_tables(physical, report, features).complete
    assert physical.calls == []


def test_dirty_control_without_dirty_capability_is_rejected() -> None:
    """HD must not be used to derive write permissions with HAFDBS=1."""
    report, features = evidence({"TCR_EL1": 39 | (39 << 16) | (2 << 30) | (1 << 40)})
    features = decode_feature_report(
        (CpuRegister("ID_AA64MMFR0_EL1", 64, 0), CpuRegister("ID_AA64MMFR1_EL1", 64, 1))
    )
    physical = Physical({})
    assert "without hardware dirty" in walk_tables(physical, report, features).reason
    assert physical.calls == []
