# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for observed AArch64 privilege and exception controls."""

from __future__ import annotations

import pytest

from pyGdbToolkit.arch import DiagnosticSeverity
from pyGdbToolkit.arch.aarch64.cpu import CpuRegister
from pyGdbToolkit.arch.aarch64.features import FeatureSupport, decode_feature_report
from pyGdbToolkit.arch.aarch64.isolation import (
    IsolationReport,
    audit_isolation,
    collect_isolation_report,
)
from pyGdbToolkit.arch.aarch64.mmu import (
    ExecutionContext,
    MmuReport,
    TranslationRegime,
    collect_execution_context,
)


class Registers:
    """Record named reads without exposing memory or writes."""

    def __init__(self, values: dict[str, int]) -> None:
        """Build a mutable register source."""
        self.values = values
        self.calls: list[tuple[str, ...]] = []

    def read_first(self, names: tuple[str, ...]) -> int | None:
        """Return the first observed alias."""
        self.calls.append(names)
        return next((self.values[name] for name in names if name in self.values), None)


@pytest.mark.parametrize("level,shift", ((2, 8), (3, 12)))
@pytest.mark.parametrize("encoding", range(16))
def test_optional_level_encodings(level: int, shift: int, encoding: int) -> None:
    """Only zero, one and two are recognized optional-EL encodings."""
    snapshot = IsolationReport(
        ExecutionContext(1, "test"),
        MmuReport(None, ()),
        (CpuRegister("ID_AA64PFR0_EL1", 64, encoding << shift),),
    )
    expected = (
        FeatureSupport.ABSENT
        if encoding == 0
        else FeatureSupport.PRESENT if encoding in (1, 2) else FeatureSupport.UNKNOWN
    )
    assert snapshot.level_support(level) is expected


@pytest.mark.parametrize("level", (2, 3))
def test_current_level_is_evidence_only_for_that_level(level: int) -> None:
    """Running at EL3 does not prove optional EL2 exists."""
    snapshot = IsolationReport(ExecutionContext(level, "test"), MmuReport(None, ()), ())
    assert snapshot.level_support(level) is FeatureSupport.PRESENT
    assert snapshot.level_support(5 - level) is FeatureSupport.UNKNOWN
    with pytest.raises(ValueError):
        snapshot.level_support(1)


@pytest.mark.parametrize("pfr0", (None, 0, 0xFF00))
def test_absent_unknown_and_reserved_levels_do_not_read_banks(pfr0: int | None) -> None:
    """Missing and absent higher-level capabilities cannot cause bank probing."""
    reader = Registers({} if pfr0 is None else {"ID_AA64PFR0_EL1": pfr0})
    snapshot = collect_isolation_report(
        reader, ExecutionContext(1, "test"), decode_feature_report(()), MmuReport(None, ())
    )
    assert all(names[1] not in ("HCR_EL2", "SCR_EL3", "PSTATE") for names in reader.calls)
    assert snapshot.bits("HCR_EL2", 0) is None


def test_existing_missing_reads_and_pstate_are_reused() -> None:
    """MMU and fallback context evidence are never retried during isolation collection."""
    reader = Registers({"pstate": 5})
    context = collect_execution_context(reader)
    mmu = MmuReport(
        TranslationRegime.EL1, (CpuRegister("HCR_EL2", 64, None), CpuRegister("SCTLR_EL1", 64, 1))
    )
    reader.values["ID_AA64PFR0_EL1"] = 0x1100
    features = decode_feature_report((CpuRegister("ID_AA64MMFR1_EL1", 64, 1 << 20),))
    snapshot = collect_isolation_report(reader, context, features, mmu)
    assert reader.calls.count(("pstate", "cpsr")) == 1
    assert ("hcr_el2", "HCR_EL2") not in reader.calls
    assert ("sctlr_el1", "SCTLR_EL1") not in reader.calls
    assert snapshot.bits("SCR_EL3", 0) is None


@pytest.mark.parametrize("pan", (0, 1))
@pytest.mark.parametrize("uao", (0, 1))
def test_pstate_pan_uao_bit_positions_and_policy(pan: int, uao: int) -> None:
    """PAN is contextual; UAO is never classified as a stand-alone policy failure."""
    features = decode_feature_report(
        (CpuRegister("ID_AA64MMFR1_EL1", 64, 1 << 20), CpuRegister("ID_AA64MMFR2_EL1", 64, 1 << 4))
    )
    mmu = MmuReport(TranslationRegime.EL1, (CpuRegister("SCTLR_EL1", 64, 1),))
    snapshot = IsolationReport(
        ExecutionContext(1, "test"),
        mmu,
        mmu.registers + (CpuRegister("PSTATE", 64, 5 | (pan << 22) | (uao << 23)),),
    )
    findings = {finding.title: finding for finding in audit_isolation(snapshot, features)}
    assert f"PAN={pan}" in findings["PSTATE.PAN"].detail
    assert f"UAO={uao}" in findings["PSTATE.UAO"].detail
    assert findings["PSTATE.PAN"].severity is (
        DiagnosticSeverity.INFO if pan else DiagnosticSeverity.WARNING
    )
    assert findings["PSTATE.UAO"].severity is DiagnosticSeverity.INFO
    assert all(finding.severity is not DiagnosticSeverity.PASS for finding in findings.values())


@pytest.mark.parametrize("pstate,valid", ((5, 64), (9, 64), (0x13, 64), (5, 22)))
def test_invalid_mismatched_or_partial_pstate(pstate: int, valid: int) -> None:
    """PAN state requires both consistent AArch64 mode and bit evidence."""
    features = decode_feature_report((CpuRegister("ID_AA64MMFR1_EL1", 64, 1 << 20),))
    snapshot = IsolationReport(
        ExecutionContext(1, "test"),
        MmuReport(None, ()),
        (CpuRegister("PSTATE", 64, pstate, valid),),
    )
    finding = next(
        finding for finding in audit_isolation(snapshot, features) if finding.title == "PSTATE.PAN"
    )
    assert finding.severity is DiagnosticSeverity.INFO
    if pstate != 5 or valid < 23:
        assert "Unavailable or inconsistent" in finding.detail


@pytest.mark.parametrize("mmfr1", (None, 0, 15 << 20))
def test_unimplemented_pan_bits_are_not_interpreted(mmfr1: int | None) -> None:
    """Reserved PSTATE bits cannot establish PAN presence or activation."""
    features = decode_feature_report((CpuRegister("ID_AA64MMFR1_EL1", 64, mmfr1),))
    snapshot = IsolationReport(
        ExecutionContext(1, "test"),
        MmuReport(None, ()),
        (CpuRegister("PSTATE", 64, 5 | (1 << 22)),),
    )
    finding = next(
        finding for finding in audit_isolation(snapshot, features) if finding.title == "PSTATE.PAN"
    )
    assert "Not interpreted" in finding.detail


@pytest.mark.parametrize("tge", (0, 1))
def test_el2_host_exception_entry_requires_tge(tge: int) -> None:
    """EL2 host layout alone does not establish PAN exception-entry behavior."""
    features = decode_feature_report((CpuRegister("ID_AA64MMFR1_EL1", 64, (3 << 20) | (1 << 8)),))
    mmu = MmuReport(
        TranslationRegime.EL2_HOST, (CpuRegister("SCTLR_EL2", 64, 1 | (1 << 23) | (1 << 57)),)
    )
    snapshot = IsolationReport(
        ExecutionContext(2, "test"),
        mmu,
        mmu.registers
        + (CpuRegister("PSTATE", 64, 9), CpuRegister("HCR_EL2", 64, (1 << 34) | (tge << 27))),
    )
    findings = {finding.title: finding for finding in audit_isolation(snapshot, features)}
    if tge:
        assert "EPAN=1" in findings["PAN on exception entry"].detail
        assert findings["PAN on exception entry"].severity is DiagnosticSeverity.WARNING
    else:
        assert "not interpreted" in findings["PAN on exception entry"].detail
        assert findings["PSTATE.PAN"].severity is DiagnosticSeverity.INFO


def test_hcr_scr_raw_controls_are_not_effective_security_states() -> None:
    """Raw guest and lower-level state controls are not global isolation verdicts."""
    reader = Registers(
        {
            "ID_AA64PFR0_EL1": 0x1100 | (1 << 36) | (1 << 52),
            "HCR_EL2": (1 << 31) | (1 << 27) | 1,
            "SCR_EL3": (1 << 62) | (1 << 18) | (1 << 10) | 1,
        }
    )
    snapshot = collect_isolation_report(
        reader, ExecutionContext(3, "test"), decode_feature_report(()), MmuReport(None, ())
    )
    findings = {
        finding.title: finding for finding in audit_isolation(snapshot, decode_feature_report(()))
    }
    assert "VM=1" in findings["EL2 configuration"].detail
    assert "TGE=1" in findings["EL2 configuration"].detail
    assert "RW=1" in findings["EL2 configuration"].detail
    assert "E2H=not interpreted" in findings["EL2 configuration"].detail
    assert "NSE=1" in findings["EL3 configuration"].detail
    assert "EEL2=1" in findings["EL3 configuration"].detail
    assert "not the execution/security state of EL3" in findings["EL3 configuration"].detail
    assert all(finding.severity is DiagnosticSeverity.INFO for finding in findings.values())


@pytest.mark.parametrize(
    "vbar,severity",
    (
        (0, DiagnosticSeverity.INFO),
        (0x800, DiagnosticSeverity.INFO),
        (0x801, DiagnosticSeverity.WARNING),
    ),
)
def test_vector_alignment_only(vbar: int, severity: DiagnosticSeverity) -> None:
    """Vector base zero is not an invalid handler, but RES0 low bits are flagged."""
    snapshot = IsolationReport(
        ExecutionContext(1, "test"), MmuReport(None, ()), (CpuRegister("VBAR_EL1", 64, vbar),)
    )
    finding = next(
        finding
        for finding in audit_isolation(snapshot, decode_feature_report(()))
        if finding.title == "Exception vector base"
    )
    assert finding.severity is severity
    assert "handler contents are not inspected" in finding.detail


def test_unknown_context_and_partial_pfr_do_not_infer_higher_levels() -> None:
    """A partial nibble cannot prove presence and an unknown EL cannot select a VBAR."""
    snapshot = IsolationReport(
        ExecutionContext(None, "test"),
        MmuReport(None, ()),
        (CpuRegister("ID_AA64PFR0_EL1", 64, 0x1100, 10),),
    )
    assert snapshot.level_support(2) is FeatureSupport.UNKNOWN
    assert snapshot.level_support(3) is FeatureSupport.UNKNOWN
    reader = Registers({"ID_AA64PFR0_EL1": 0x1100})
    collect_isolation_report(reader, snapshot.context, decode_feature_report(()), snapshot.mmu)
    assert reader.calls == [("id_aa64pfr0_el1", "ID_AA64PFR0_EL1")]


@pytest.mark.parametrize(
    "name,shift",
    (
        ("VM", 0),
        ("FMO", 3),
        ("IMO", 4),
        ("AMO", 5),
        ("DC", 12),
        ("TVM", 26),
        ("TGE", 27),
        ("TRVM", 30),
        ("RW", 31),
    ),
)
def test_hcr_field_positions(name: str, shift: int) -> None:
    """Independent HCR vectors preserve routing and trap encodings."""
    snapshot = IsolationReport(
        ExecutionContext(2, "test"), MmuReport(None, ()), (CpuRegister("HCR_EL2", 64, 1 << shift),)
    )
    finding = next(
        finding
        for finding in audit_isolation(snapshot, decode_feature_report(()))
        if finding.title == "EL2 configuration"
    )
    assert f"{name}=1" in finding.detail
    assert finding.severity is DiagnosticSeverity.INFO


@pytest.mark.parametrize(
    "name,shift", (("NS", 0), ("IRQ", 1), ("FIQ", 2), ("EA", 3), ("SMD", 7), ("RW", 10))
)
def test_scr_field_positions_and_optional_gating(name: str, shift: int) -> None:
    """SCR policy bits do not imply a current Security state or optional extensions."""
    snapshot = IsolationReport(
        ExecutionContext(3, "test"),
        MmuReport(None, ()),
        (CpuRegister("SCR_EL3", 64, (1 << shift) | (1 << 18) | (1 << 62)),),
    )
    finding = next(
        finding
        for finding in audit_isolation(snapshot, decode_feature_report(()))
        if finding.title == "EL3 configuration"
    )
    assert f"{name}=1" in finding.detail
    assert "EEL2=" not in finding.detail
    assert "NSE=" not in finding.detail
    assert "HCE=" not in finding.detail
    assert finding.severity is DiagnosticSeverity.INFO


@pytest.mark.parametrize("sctlr", (None, 0))
def test_pan_zero_with_unavailable_or_disabled_translation(sctlr: int | None) -> None:
    """The instantaneous PAN bit is not a risk verdict without active MMU evidence."""
    mmu = MmuReport(TranslationRegime.EL1, (CpuRegister("SCTLR_EL1", 64, sctlr),))
    snapshot = IsolationReport(
        ExecutionContext(1, "test"), mmu, mmu.registers + (CpuRegister("PSTATE", 64, 5),)
    )
    features = decode_feature_report((CpuRegister("ID_AA64MMFR1_EL1", 64, 1 << 20),))
    finding = next(
        finding for finding in audit_isolation(snapshot, features) if finding.title == "PSTATE.PAN"
    )
    assert finding.severity is DiagnosticSeverity.INFO


def test_collection_is_fresh_and_signed_values_are_masked() -> None:
    """CPU selection changes cannot retain a previous isolation snapshot."""
    reader = Registers({"ID_AA64PFR0_EL1": 0x1100, "SCR_EL3": -1})
    context = ExecutionContext(3, "test")
    features = decode_feature_report(())
    first = collect_isolation_report(reader, context, features, MmuReport(None, ()))
    reader.values["SCR_EL3"] = 0
    second = collect_isolation_report(reader, context, features, MmuReport(None, ()))
    assert first.bits("SCR_EL3", 0, 64) == (1 << 64) - 1
    assert second.bits("SCR_EL3", 0, 64) == 0
    assert reader.calls.count(("scr_el3", "SCR_EL3")) == 2
