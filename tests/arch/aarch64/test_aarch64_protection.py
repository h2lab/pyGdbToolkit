# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for conditional PAC, BTI and MTE configuration observations."""

from __future__ import annotations

import pytest

from pyGdbToolkit.arch import DiagnosticFinding, DiagnosticSeverity
from pyGdbToolkit.arch.aarch64.cpu import CpuRegister
from pyGdbToolkit.arch.aarch64.features import FeatureReport, FeatureSupport, decode_feature_report
from pyGdbToolkit.arch.aarch64.isolation import IsolationReport
from pyGdbToolkit.arch.aarch64.mmu import ExecutionContext, MmuReport, TranslationRegime
from pyGdbToolkit.arch.aarch64.protection import (
    audit_protection,
    collect_protection_report,
    pac_support,
)


def features(values: dict[str, int]) -> FeatureReport:
    """Build independent feature register evidence."""
    return decode_feature_report(
        tuple(CpuRegister(name, 64, value) for name, value in values.items())
    )


def snapshot(
    values: dict[str, int], regime: TranslationRegime | None = TranslationRegime.EL1
) -> IsolationReport:
    """Supply controls without target memory or key-register interfaces."""
    level = (
        None
        if regime is None
        else {
            TranslationRegime.EL1: 1,
            TranslationRegime.EL2: 2,
            TranslationRegime.EL2_HOST: 2,
            TranslationRegime.EL3: 3,
        }[regime]
    )
    registers = tuple(CpuRegister(name, 64, value) for name, value in values.items())
    return IsolationReport(ExecutionContext(level, "test"), MmuReport(regime, registers), registers)


def observations(report: IsolationReport, caps: FeatureReport) -> dict[str, DiagnosticFinding]:
    """Index portable findings by their stable titles."""
    return {finding.title: finding for finding in audit_protection(report, caps)}


@pytest.mark.parametrize("regime", tuple(TranslationRegime))
@pytest.mark.parametrize("name,shift", (("EnIA", 31), ("EnIB", 30), ("EnDA", 27), ("EnDB", 13)))
def test_pac_enable_positions(regime: TranslationRegime, name: str, shift: int) -> None:
    """All bank layouts retain the same PAC address enable positions."""
    caps = features({"ID_AA64ISAR1_EL1": 1 << 4})
    result = observations(snapshot({f"SCTLR_{regime.suffix}": 1 << shift}, regime), caps)
    assert f"{name}=1" in result["Address authentication controls"].detail
    assert result["Address authentication controls"].severity is DiagnosticSeverity.INFO
    assert all(finding.severity is not DiagnosticSeverity.PASS for finding in result.values())


@pytest.mark.parametrize(
    "values,state",
    (
        ({}, FeatureSupport.UNKNOWN),
        ({"ID_AA64ISAR1_EL1": 0, "ID_AA64ISAR2_EL1": 0}, FeatureSupport.ABSENT),
        ({"ID_AA64ISAR1_EL1": 1 << 4}, FeatureSupport.PRESENT),
        ({"ID_AA64ISAR2_EL1": 1 << 12}, FeatureSupport.PRESENT),
        ({"ID_AA64ISAR1_EL1": (1 << 4) | (1 << 8)}, FeatureSupport.UNKNOWN),
        ({"ID_AA64ISAR1_EL1": 15 << 4}, FeatureSupport.UNKNOWN),
    ),
)
def test_address_algorithm_aggregation(values: dict[str, int], state: FeatureSupport) -> None:
    """Alternative algorithms do not convert missing or conflicting evidence to absence."""
    assert (
        pac_support(
            features(values),
            ("PAC address QARMA5", "PAC address implementation-defined", "PAC address QARMA3"),
        )
        is state
    )


def test_generic_pac_does_not_enable_address_controls() -> None:
    """PACGA capability is distinct from address authentication."""
    result = observations(
        snapshot({"SCTLR_EL1": -1}), features({"ID_AA64ISAR1_EL1": 1 << 24, "ID_AA64ISAR2_EL1": 0})
    )
    assert "generic PACGA support: present" in result["Address authentication controls"].detail
    assert "EnIA=" not in result["Address authentication controls"].detail


def test_pac_disabled_and_missing_controls_are_distinct() -> None:
    """Only all four observed disabled bits produce a configuration warning."""
    caps = features({"ID_AA64ISAR1_EL1": 1 << 4})
    assert (
        observations(snapshot({}), caps)["Address authentication controls"].severity
        is DiagnosticSeverity.INFO
    )
    assert (
        observations(snapshot({"SCTLR_EL1": 0}), caps)["Address authentication controls"].severity
        is DiagnosticSeverity.WARNING
    )


@pytest.mark.parametrize("name,shift", (("BT0", 35), ("BT1", 36)))
def test_bti_compatibility_not_global_enable(name: str, shift: int) -> None:
    """Zero and one BT values are compatibility choices, not BTI PASS/FAIL."""
    caps = features({"ID_AA64PFR1_EL1": 1})
    for value in (0, 1 << shift):
        finding = observations(snapshot({"SCTLR_EL1": value}), caps)["Branch target compatibility"]
        assert f"{name}={int(bool(value))}" in finding.detail
        assert "not global BTI enable" in finding.detail
        assert finding.severity is DiagnosticSeverity.INFO


@pytest.mark.parametrize("regime", (TranslationRegime.EL2, TranslationRegime.EL3))
def test_single_bank_bti_and_mte_skip_el0_fields(regime: TranslationRegime) -> None:
    """Classic EL2 and EL3 have neither BT0 nor TCF0 observations."""
    result = observations(
        snapshot({f"SCTLR_{regime.suffix}": 1 << 36}, regime),
        features({"ID_AA64PFR1_EL1": 1 | (2 << 8)}),
    )
    assert "BT=1" in result["Branch target compatibility"].detail
    assert "BT0=" not in result["Branch target compatibility"].detail
    assert "EL0 tag checking controls" not in result


@pytest.mark.parametrize("mte", (0, 1, 4, 15))
def test_no_tag_control_decoding_without_mte2(mte: int) -> None:
    """Absent, instruction-only and reserved MTE do not activate runtime tag fields."""
    result = observations(
        snapshot({"SCTLR_EL1": -1, "PSTATE": -1}), features({"ID_AA64PFR1_EL1": mte << 8})
    )
    assert "ATA=" not in result["Tag checking configuration"].detail
    assert "Tag check override" not in result
    assert all(finding.severity is DiagnosticSeverity.INFO for finding in result.values())


@pytest.mark.parametrize("mte", (2, 3))
@pytest.mark.parametrize("mode", range(4))
def test_tag_fault_modes(mte: int, mode: int) -> None:
    """TCF at 41:40 and TCF0 at 39:38 are distinct and mode 3 requires MTE3."""
    result = observations(
        snapshot({"SCTLR_EL1": (mode << 40) | (1 << 38)}), features({"ID_AA64PFR1_EL1": mte << 8})
    )
    assert f"TCF={mode}" in result["Tag checking configuration"].detail
    assert "TCF0=1" in result["EL0 tag checking controls"].detail
    expected = DiagnosticSeverity.WARNING if mte == 2 and mode == 3 else DiagnosticSeverity.INFO
    assert result["Tag checking configuration"].severity is expected
    if mode == 3:
        assert ("reserved without MTE3" if mte == 2 else "asymmetric") in result[
            "Tag checking configuration"
        ].detail


@pytest.mark.parametrize("regime", tuple(TranslationRegime))
def test_mte_ata_and_tcf_positions_all_banks(regime: TranslationRegime) -> None:
    """Privileged TCF and ATA retain their positions at EL1, EL2 and EL3."""
    result = observations(
        snapshot({f"SCTLR_{regime.suffix}": (1 << 43) | (1 << 40)}, regime),
        features({"ID_AA64PFR1_EL1": 2 << 8}),
    )
    assert "ATA=1, TCF=1" in result["Tag checking configuration"].detail


@pytest.mark.parametrize("pstate", (5, 5 | (1 << 25), 9, 0x13))
def test_tco_requires_consistent_pstate(pstate: int) -> None:
    """TCO is bit25 but only a matching AArch64 mode supplies its state."""
    result = observations(snapshot({"PSTATE": pstate}), features({"ID_AA64PFR1_EL1": 2 << 8}))
    expected = str((pstate >> 25) & 1) if pstate & 31 == 5 else "unknown"
    assert f"PSTATE.TCO={expected}" in result["Tag check override"].detail


def test_partial_registers_do_not_fabricate_mte_controls() -> None:
    """Low-word evidence cannot become ATA, TCF or a readable TCO bit."""
    report = snapshot({})
    report = IsolationReport(
        report.context,
        report.mmu,
        (CpuRegister("SCTLR_EL1", 64, 0, 32), CpuRegister("PSTATE", 64, 5, 25)),
    )
    result = observations(report, features({"ID_AA64PFR1_EL1": 2 << 8}))
    assert "ATA=unknown, TCF=unknown" in result["Tag checking configuration"].detail
    assert "PSTATE.TCO=unknown" in result["Tag check override"].detail


@pytest.mark.parametrize("tge", (0, 1))
def test_el2_host_el0_fields_require_tge(tge: int) -> None:
    """VHE layout alone does not make host EL0 controls effective."""
    result = observations(
        snapshot({"HCR_EL2": (1 << 34) | (tge << 27)}, TranslationRegime.EL2_HOST),
        features({"ID_AA64PFR1_EL1": 1 | (2 << 8)}),
    )
    assert ("BT0=" in result["Branch target compatibility"].detail) == bool(tge)
    assert ("EL0 tag checking controls" in result) == bool(tge)


def test_higher_level_trap_and_tag_gates() -> None:
    """API/APK and ATA use their independent HCR/SCR encodings without keys."""
    report = snapshot(
        {
            "ID_AA64PFR0_EL1": 0x1100,
            "HCR_EL2": (1 << 41) | (1 << 56),
            "SCR_EL3": (1 << 16) | (1 << 26),
        }
    )
    caps = features({"ID_AA64ISAR1_EL1": 1 << 4, "ID_AA64PFR1_EL1": 2 << 8})
    result = observations(report, caps)
    assert "HCR_EL2: API=1, APK=0" in result["Authentication trap controls"].detail
    assert "SCR_EL3: API=0, APK=1" in result["Authentication trap controls"].detail
    assert "HCR_EL2: ATA=1" in result["Higher-level tag access controls"].detail
    assert "SCR_EL3: ATA=1" in result["Higher-level tag access controls"].detail


@pytest.mark.parametrize("fraction", (0, 15, 1))
def test_mte_asynchronous_mode_fraction(fraction: int) -> None:
    """MTE2 asynchronous capability is not assumed when MTE_frac is absent/reserved."""
    result = observations(
        snapshot({"SCTLR_EL1": 2 << 40}), features({"ID_AA64PFR1_EL1": (2 << 8) | (fraction << 40)})
    )
    assert ("mode support unknown/absent" in result["Tag checking configuration"].detail) == (
        fraction != 0
    )


def test_collection_never_requests_keys_or_new_banks() -> None:
    """The only permitted new read is PSTATE for MTE2+, once per snapshot."""

    class Reader:
        """Reject any request other than the allowed PSTATE aliases."""

        def __init__(self) -> None:
            """Initialize the request count."""
            self.calls = 0

        def read_first(self, names: tuple[str, ...]) -> int | None:
            """Return missing PSTATE evidence without probing anything else."""
            assert names == ("pstate", "cpsr")
            self.calls += 1
            return None

    reader = Reader()
    for caps in (
        features({}),
        features({"ID_AA64PFR1_EL1": 0}),
        features({"ID_AA64PFR1_EL1": 1 << 8}),
    ):
        assert collect_protection_report(reader, snapshot({}), caps) == snapshot({})
    caps = features({"ID_AA64PFR1_EL1": 2 << 8})
    first = collect_protection_report(reader, snapshot({}), caps)
    assert collect_protection_report(reader, first, caps) == first
    assert reader.calls == 1
    collect_protection_report(reader, snapshot({}), caps)
    assert reader.calls == 2


def test_unknown_regime_never_probes_or_decodes_control_bank() -> None:
    """A capable target still needs an identified regime for configuration decoding."""
    caps = features({"ID_AA64ISAR1_EL1": 1 << 4, "ID_AA64PFR1_EL1": 1 | (2 << 8)})
    result = observations(snapshot({}, None), caps)
    assert len(result) == 4
    assert all(finding.severity is DiagnosticSeverity.INFO for finding in result.values())


@pytest.mark.parametrize("name,shift", (("TBI0", 37), ("TBI1", 38), ("TCMA0", 57), ("TCMA1", 58)))
def test_dual_range_tag_address_fields(name: str, shift: int) -> None:
    """EL1-style TCR fields keep their independent address tagging positions."""
    caps = features({"ID_AA64PFR1_EL1": 2 << 8})
    result = observations(snapshot({"TCR_EL1": 1 << shift}), caps)
    assert f"{name}=1" in result["Tag address controls"].detail


@pytest.mark.parametrize("regime", (TranslationRegime.EL2, TranslationRegime.EL3))
@pytest.mark.parametrize("name,shift", (("TBI", 20), ("TCMA", 30)))
def test_single_range_tag_address_fields(regime: TranslationRegime, name: str, shift: int) -> None:
    """Single-range TCR uses different tagging field locations."""
    caps = features({"ID_AA64PFR1_EL1": 2 << 8})
    result = observations(snapshot({f"TCR_{regime.suffix}": 1 << shift}, regime), caps)
    assert f"{name}=1" in result["Tag address controls"].detail
    assert "TCMA0=" not in result["Tag address controls"].detail


def test_reserved_el0_fault_mode_and_ata0_bit() -> None:
    """The separate EL0 TCF0 field also requires MTE3 for asymmetric mode."""
    result = observations(
        snapshot({"SCTLR_EL1": (3 << 38) | (1 << 42)}), features({"ID_AA64PFR1_EL1": 2 << 8})
    )
    assert "ATA0=1, TCF0=3" in result["EL0 tag checking controls"].detail
    assert result["EL0 tag checking controls"].severity is DiagnosticSeverity.WARNING


def test_higher_level_banks_absent_or_unknown_are_not_interpreted() -> None:
    """Supplied raw bank values do not bypass capability qualification."""
    caps = features({"ID_AA64PFR1_EL1": 2 << 8, "ID_AA64ISAR1_EL1": 1 << 4})
    for values in (
        {"HCR_EL2": -1, "SCR_EL3": -1},
        {"ID_AA64PFR0_EL1": 0, "HCR_EL2": -1, "SCR_EL3": -1},
    ):
        result = observations(snapshot(values), caps)
        assert "Higher-level tag access controls" not in result
        assert "Authentication trap controls" not in result
