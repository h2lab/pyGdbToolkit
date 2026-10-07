# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the configuration-only AArch64 MMU audit."""

from __future__ import annotations

import pytest

from pyGdbToolkit.arch import DiagnosticFinding, DiagnosticSeverity
from pyGdbToolkit.arch.aarch64.cpu import CpuRegister
from pyGdbToolkit.arch.aarch64.features import FeatureReport, decode_feature_report
from pyGdbToolkit.arch.aarch64.mmu import (
    ExecutionContext,
    MmuReport,
    TranslationRegime,
    audit_mmu,
    collect_execution_context,
    collect_mmu_report,
)


class Registers:
    """Expose named registers and record every request."""

    def __init__(self, values: dict[str, int]) -> None:
        """Initialize a portable register source."""
        self.values = values
        self.calls: list[tuple[str, ...]] = []

    def read_first(self, names: tuple[str, ...]) -> int | None:
        """Read only configured aliases."""
        self.calls.append(names)
        return next((self.values[name] for name in names if name in self.values), None)


def features(mmfr0: int | None = None, mmfr1: int | None = None) -> FeatureReport:
    """Build a feature snapshot independently of MMU register access."""
    return decode_feature_report(
        (CpuRegister("ID_AA64MMFR0_EL1", 64, mmfr0), CpuRegister("ID_AA64MMFR1_EL1", 64, mmfr1))
    )


def findings(
    values: dict[str, int],
    mmfr0: int | None = None,
    mmfr1: int | None = None,
    regime: TranslationRegime = TranslationRegime.EL1,
) -> dict[str, DiagnosticFinding]:
    """Decode an injected configuration without memory access."""
    report = MmuReport(
        regime, tuple(CpuRegister(name, 64, value) for name, value in values.items())
    )
    return {finding.title: finding for finding in audit_mmu(report, features(mmfr0, mmfr1))}


@pytest.mark.parametrize(
    "level,mmfr1,hcr,regime,count",
    (
        (1, None, None, TranslationRegime.EL1, 5),
        (3, None, None, TranslationRegime.EL3, 4),
        (2, 0, None, TranslationRegime.EL2, 4),
        (2, 1 << 8, 0, TranslationRegime.EL2, 5),
        (2, 1 << 8, 1 << 34, TranslationRegime.EL2_HOST, 6),
        (2, None, 0, TranslationRegime.EL2, 5),
    ),
)
def test_regime_selection(
    level: int, mmfr1: int | None, hcr: int | None, regime: TranslationRegime, count: int
) -> None:
    """The selected EL and observed E2H choose the correct named register bank."""
    reader = Registers({} if hcr is None else {"HCR_EL2": hcr})
    report = collect_mmu_report(reader, ExecutionContext(level, "test"), features(mmfr1=mmfr1))
    assert report.regime is regime
    assert len(reader.calls) == count
    assert (
        f"TTBR1_{regime.suffix}" in {register.name for register in report.registers}
        if regime.dual_range
        else f"TTBR1_{regime.suffix}" not in {register.name for register in report.registers}
    )
    assert all("_EL1" not in names[1] for names in reader.calls) if level != 1 else True


@pytest.mark.parametrize(
    "level,mmfr1,hcr", ((None, None, None), (0, None, None), (2, None, None), (2, None, 1 << 34))
)
def test_ambiguous_context_does_not_probe_mmu_banks(
    level: int | None, mmfr1: int | None, hcr: int | None
) -> None:
    """Unknown EL and host state cannot become a guessed translation layout."""
    reader = Registers({} if hcr is None else {"HCR_EL2": hcr})
    report = collect_mmu_report(reader, ExecutionContext(level, "test"), features(mmfr1=mmfr1))
    assert report.regime is None
    assert report.unavailable_reason
    assert all(names == ("hcr_el2", "HCR_EL2") for names in reader.calls)
    assert all(
        finding.severity is DiagnosticSeverity.INFO for finding in audit_mmu(report, features())
    )


def test_partial_fields_and_unsigned_collection() -> None:
    """Partial control reads do not fabricate high bits; signed full reads are masked."""
    report = MmuReport(TranslationRegime.EL1, (CpuRegister("TCR_EL1", 64, 0, 32),))
    assert report.bits("TCR_EL1", 0, 6) == 0
    assert report.bits("TCR_EL1", 32, 3) is None
    assert report.bits("SCTLR_EL1", 0) is None
    reader = Registers({"SCTLR_EL1": -1})
    snapshot = collect_mmu_report(reader, ExecutionContext(1, "test"), features())
    assert snapshot.bits("SCTLR_EL1", 0, 64) == (1 << 64) - 1


@pytest.mark.parametrize("values", ({}, {"SCTLR_EL1": 0}, {"SCTLR_EL1": 1 << 19}))
def test_unavailable_or_disabled_mmu_has_no_consistency_pass(values: dict[str, int]) -> None:
    """Inactive geometry never receives validation and WXN never proves W^X."""
    result = findings(values | {"TCR_EL1": (3 << 14) | (1 << 12)})
    assert "MMU consistency checks skipped" in result
    assert "Translation range 0" not in result
    assert all(finding.severity is not DiagnosticSeverity.PASS for finding in result.values())


@pytest.mark.parametrize(
    "regime,shift",
    (
        (TranslationRegime.EL1, 32),
        (TranslationRegime.EL2, 16),
        (TranslationRegime.EL2_HOST, 32),
        (TranslationRegime.EL3, 16),
    ),
)
def test_output_size_layout_and_cpu_limit(regime: TranslationRegime, shift: int) -> None:
    """IPS and PS occupy different fields and only known PARange permits comparison."""
    values = {f"SCTLR_{regime.suffix}": 1, f"TCR_{regime.suffix}": 5 << shift}
    assert (
        findings(values, 2, regime=regime)["Output address size"].severity
        is DiagnosticSeverity.ERROR
    )
    assert (
        findings(values, None, regime=regime)["Output address size"].severity
        is DiagnosticSeverity.INFO
    )


@pytest.mark.parametrize(
    "index,encoding,granule", ((0, 0, 4), (0, 1, 64), (0, 2, 16), (1, 1, 16), (1, 2, 4), (1, 3, 64))
)
def test_granule_encodings(index: int, encoding: int, granule: int) -> None:
    """TG0 and TG1 have distinct encodings, including the supported 16 KiB case."""
    value = encoding << (30 if index else 14)
    result = findings({"SCTLR_EL1": 1, "TCR_EL1": value}, mmfr0=1 << 20)
    assert f"TG{index}={granule} KiB" in result[f"Translation range {index}"].detail
    assert "support present" in result[f"Granule support {index}"].detail


def test_unsupported_granule_and_reserved_shareability() -> None:
    """A known unsupported size and reserved SH produce distinct findings."""
    result = findings({"SCTLR_EL1": 1, "TCR_EL1": 1 << 12}, mmfr0=15 << 28)
    assert result["Granule support 0"].severity is DiagnosticSeverity.WARNING
    assert result["Table-walk shareability 0"].severity is DiagnosticSeverity.ERROR


def test_disabled_walks_skip_geometry_without_claiming_no_cached_translation() -> None:
    """EPD disables misses, not existing TLB translations."""
    result = findings({"SCTLR_EL1": 1, "TCR_EL1": (1 << 7) | (3 << 14)})
    assert "cached translations" in result["Translation range 0"].detail
    assert "Granule support 0" not in result


@pytest.mark.parametrize("ps", (6, 7))
def test_extended_sizes_are_not_mistaken_for_base_formats(ps: int) -> None:
    """52/56-bit output sizes are not naively compared as ordinary IPS."""
    result = findings({"SCTLR_EL1": 1, "TCR_EL1": ps << 32}, mmfr0=5)
    assert result["Output address size"].severity is DiagnosticSeverity.INFO
    assert "not qualified" in result["Output address size"].detail


def test_optional_controls_require_feature_evidence() -> None:
    """HA/HD and HPD are interpreted only for positively identified features."""
    values = {"SCTLR_EL1": 1, "TCR_EL1": (1 << 40) | (1 << 41)}
    for mmfr1 in (None, 0, 15 | (15 << 12)):
        result = findings(values, mmfr1=mmfr1)
        assert "not interpreted" in result["Hardware access/dirty updates"].detail
        assert "not interpreted" in result["Hierarchical permissions 0"].detail
    result = findings(values, mmfr1=2 | (1 << 12))
    assert result["Hardware access/dirty updates"].severity is DiagnosticSeverity.WARNING
    assert result["Hierarchical permissions 0"].severity is DiagnosticSeverity.WARNING


def test_zero_ttbr_and_mair_are_observations_not_mapping_verdicts() -> None:
    """Physical table base zero and unused MAIR slots are not necessarily invalid."""
    result = findings({"SCTLR_EL1": 1 | (1 << 19), "TTBR0_EL1": 0, "MAIR_EL1": 0})
    assert result["TTBR0_EL1"].severity is DiagnosticSeverity.INFO
    assert result["MAIR_EL1"].severity is DiagnosticSeverity.INFO
    assert all(finding.severity is not DiagnosticSeverity.PASS for finding in result.values())


def test_context_reads_are_fresh_and_invalid_currentel_does_not_fallback() -> None:
    """A bad CurrentEL cannot be hidden by another register."""
    reader = Registers({"CurrentEL": 1, "pstate": 5})
    assert collect_execution_context(reader).exception_level is None
    assert reader.calls == [("currentel", "CurrentEL")]
    reader.values = {"pstate": 0x60000085}
    assert collect_execution_context(reader).exception_level == 1


@pytest.mark.parametrize("regime", tuple(TranslationRegime))
def test_extended_descriptor_formats_skip_classic_geometry(regime: TranslationRegime) -> None:
    """DS=1 changes the relevance of SH and output address encodings."""
    ds_shift = 59 if regime.dual_range else 32
    values = {
        f"SCTLR_{regime.suffix}": 1,
        f"TCR_{regime.suffix}": (1 << ds_shift) | (1 << 12),
    }
    result = findings(values, regime=regime)
    assert result["Extended translation format"].severity is DiagnosticSeverity.INFO
    assert "Table-walk shareability 0" not in result


def test_partial_tcr_does_not_assume_classic_descriptor_format() -> None:
    """Missing DS evidence prevents a default classic-layout verdict."""
    report = MmuReport(
        TranslationRegime.EL1,
        (
            CpuRegister("SCTLR_EL1", 64, 1),
            CpuRegister("TCR_EL1", 64, 1 << 12, 32),
        ),
    )
    result = {finding.title: finding for finding in audit_mmu(report, features())}
    assert "Extended translation format" in result
    assert "Table-walk shareability 0" not in result


def test_missing_ha_does_not_hide_available_low_mmu_controls() -> None:
    """Partial SCTLR evidence can still establish M without fabricating WXN."""
    report = MmuReport(TranslationRegime.EL3, (CpuRegister("SCTLR_EL3", 64, 1, 1),))
    result = {finding.title: finding for finding in audit_mmu(report, features())}
    assert "M=1" in result["Stage-1 MMU control"].detail
    assert "unreadable" in result["Write-implies-execute-never control"].detail
