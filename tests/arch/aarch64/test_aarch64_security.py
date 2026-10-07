# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Tests for the initial AArch64 security-audit increment."""

from __future__ import annotations

import pytest

from pyGdbToolkit.arch import (
    Architecture,
    DEFAULT_DIAGNOSTIC_RUNTIME,
    DiagnosticResult,
    DiagnosticRuntimeAccess,
    DiagnosticServiceName,
    DiagnosticSeverity,
    TargetDescription,
)
from pyGdbToolkit.arch.aarch64.security import AArch64SecurityAuditor
from pyGdbToolkit.arch.aarch64.features import FEATURE_FIELDS, FEATURE_REGISTERS
from pyGdbToolkit.arch.aarch64.target import AArch64TargetDescription
from pyGdbToolkit.cmd_secscan import SecscanReport, run_audit
from pyGdbToolkit.session import ToolkitSession
from pyGdbToolkit.target_memory import TargetMemory


class MemoryReader:
    """Identify AArch64 and reject all memory reads."""

    architecture_name = "aarch64"

    def read_bytes(self, address: int, size: int) -> bytes:
        """Reject byte reads."""
        raise AssertionError((address, size))

    def read_uint16(self, address: int) -> int:
        """Reject halfword reads."""
        raise AssertionError((address, 2))

    def read_uint32(self, address: int) -> int:
        """Reject word reads."""
        raise AssertionError((address, 4))


class Registers:
    """Expose only configured register names and retain all requests."""

    def __init__(self, values: dict[str, int]) -> None:
        """Create an injectable register source."""
        self.values = values
        self.calls: list[tuple[str, ...]] = []

    def read_first(self, names: tuple[str, ...]) -> int | None:
        """Return the first configured alias."""
        self.calls.append(names)
        return next((self.values[name] for name in names if name in self.values), None)


def target() -> AArch64TargetDescription:
    """Build the architecture probe's minimal description."""
    return AArch64TargetDescription(Architecture.AARCH64, "Arm", "AArch64", "unknown", "aarch64")


def test_default_runtime_dispatches_without_memory_reads() -> None:
    """AArch64 reaches its own service and reports observed context only."""
    registers = Registers({"MIDR_EL1": 0x410FD034, "CurrentEL": 4})
    result = DEFAULT_DIAGNOSTIC_RUNTIME.diagnose(
        MemoryReader(),
        DiagnosticServiceName.SECURITY_AUDIT,
        DiagnosticRuntimeAccess(registers=registers),
    )
    assert result.report is not None
    assert result.report.target == target()
    assert all(finding.severity is DiagnosticSeverity.INFO for finding in result.report.findings)
    assert "Cortex-A53 r0p4" in result.report.findings[1].detail
    assert result.report.findings[2].detail == "EL1, observed through CurrentEL."
    assert registers.calls == [
        *((name.lower(), name) for name in FEATURE_REGISTERS),
        ("midr_el1", "MIDR_EL1"),
        ("currentel", "CurrentEL"),
        ("sctlr_el1", "SCTLR_EL1"),
        ("tcr_el1", "TCR_EL1"),
        ("ttbr0_el1", "TTBR0_EL1"),
        ("mair_el1", "MAIR_EL1"),
        ("ttbr1_el1", "TTBR1_EL1"),
        ("id_aa64pfr0_el1", "ID_AA64PFR0_EL1"),
        ("vbar_el1", "VBAR_EL1"),
    ]


def test_missing_registers_do_not_invent_cpu_version_or_security() -> None:
    """A report remains available without identifying a CPU or declaring protection."""
    report = AArch64SecurityAuditor().collect(MemoryReader(), target())
    assert len(report.findings) == 20 + len(FEATURE_FIELDS)
    assert all(finding.severity is DiagnosticSeverity.INFO for finding in report.findings)
    assert "not audited" in report.findings[0].detail
    assert "identity is unknown" in report.findings[1].detail
    assert "EL is unknown" in report.findings[2].detail
    assert report.findings[3].title == "ARMv8-A minor version is undetermined"


@pytest.mark.parametrize("current_el", (0, 4, 8, 12))
def test_current_el_encodings(current_el: int) -> None:
    """All four architected CurrentEL encodings are decoded."""
    report = AArch64SecurityAuditor(Registers({"currentel": current_el})).collect(
        MemoryReader(), target()
    )
    assert report.findings[2].detail == f"EL{current_el >> 2}, observed through CurrentEL."


@pytest.mark.parametrize("pstate", (0, 4, 5, 8, 9, 12, 13, 0x60000085, -2147483643))
def test_pstate_fallback(pstate: int) -> None:
    """A valid AArch64 mode supplies EL when CurrentEL is hidden."""
    report = AArch64SecurityAuditor(Registers({"pstate": pstate})).collect(MemoryReader(), target())
    assert report.findings[2].detail == (
        f"EL{(pstate & 0xC) >> 2}, observed through GDB PSTATE.M[3:2]."
    )


@pytest.mark.parametrize("values", ({}, {"CurrentEL": 1}, {"pstate": 0x13}, {"pstate": 2}))
def test_unavailable_or_invalid_context_is_unknown(values: dict[str, int]) -> None:
    """AArch32 modes and invalid encodings never become an AArch64 EL."""
    report = AArch64SecurityAuditor(Registers(values)).collect(MemoryReader(), target())
    assert "EL is unknown" in report.findings[2].detail


def test_runtime_access_overrides_injected_reader_and_is_fresh() -> None:
    """The selected runtime context takes precedence and is reread for each audit."""
    fallback = Registers({"CurrentEL": 12})
    registers = Registers({"CurrentEL": 4})
    auditor = AArch64SecurityAuditor(fallback)
    access = DiagnosticRuntimeAccess(registers=registers)
    first = auditor.collect(MemoryReader(), target(), access)
    registers.values["CurrentEL"] = 8
    second = auditor.collect(MemoryReader(), target(), access)
    assert "EL1," in first.findings[2].detail
    assert "EL2," in second.findings[2].detail
    assert fallback.calls == []


def test_service_rejects_other_architectures() -> None:
    """ARM targets cannot enter the AArch64 collector."""
    arm = TargetDescription(Architecture.ARM, "Arm", "Cortex-M4", "r0p1")
    auditor = AArch64SecurityAuditor()
    assert not auditor.supports(arm)
    with pytest.raises(ValueError, match="requires an AArch64 target"):
        auditor.collect(MemoryReader(), arm)


def test_secscan_command_renders_aarch64_report_with_stable_schema(fake_gdb: object) -> None:
    """The common command and JSON model accept the registered AArch64 report."""
    fake_gdb._inferior = object()

    class Runtime:
        """Delegate to the real registry using metadata-only target memory."""

        def diagnose(
            self,
            reader: TargetMemory,
            service: DiagnosticServiceName,
            access: DiagnosticRuntimeAccess | None = None,
        ) -> DiagnosticResult:
            """Exercise the real probe and service without hardware."""
            del reader
            return DEFAULT_DIAGNOSTIC_RUNTIME.diagnose(MemoryReader(), service, access)

    report = run_audit(ToolkitSession(diagnostic_runtime=Runtime()))
    assert report.core == "AArch64"
    assert report.vendor is None
    assert report.device_name is None
    assert report.counts() == {"FAIL": 0, "WARN": 0, "INFO": 20 + len(FEATURE_FIELDS), "PASS": 0}
    assert SecscanReport.from_dict(report.to_dict()) == report


def test_capability_findings_preserve_evidence_without_security_verdicts() -> None:
    """The common report distinguishes variants, unknowns and feature history."""
    registers = Registers(
        {
            "ID_AA64MMFR1_EL1": 2 << 20,
            "ID_AA64PFR1_EL1": (1 << 8) | 15,
        }
    )
    report = AArch64SecurityAuditor(registers).collect(MemoryReader(), target())
    capabilities = {finding.title: finding for finding in report.findings[4:]}
    assert "present: PAN2" in capabilities["PAN"].detail
    assert "ID_AA64MMFR1_EL1.PAN=0x2" in capabilities["PAN"].detail
    assert "instructions only" in capabilities["MTE"].detail
    assert "unknown:" in capabilities["BTI"].detail
    assert "Reserved or unrecognized" in capabilities["BTI"].detail
    assert "unknown:" in capabilities["UAO"].detail
    assert "ARMv8.1-A, ARMv8.5-A" in report.findings[3].detail
    assert "not a minimum or exact" in report.findings[3].detail
    assert all(finding.severity is DiagnosticSeverity.INFO for finding in report.findings)


def test_mmu_findings_are_appended_without_memory_access() -> None:
    """The neutral report contains MMU configuration warnings and retains feature findings."""
    registers = Registers(
        {
            "CurrentEL": 4,
            "SCTLR_EL1": 1,
            "TCR_EL1": (2 << 30) | (16 << 16) | 16,
            "ID_AA64MMFR0_EL1": 5,
        }
    )
    report = AArch64SecurityAuditor(registers).collect(MemoryReader(), target())
    mmu = {finding.title: finding for finding in report.findings if finding.category == "MMU"}
    assert mmu["Stage-1 MMU control"].severity is DiagnosticSeverity.INFO
    assert mmu["Write-implies-execute-never control"].severity is DiagnosticSeverity.WARNING
    assert "No translation tables" in mmu["MMU audit scope"].detail
    assert len(
        [finding for finding in report.findings if finding.category == "Capabilities"]
    ) == len(FEATURE_FIELDS)
    assert registers.calls.count(("currentel", "CurrentEL")) == 1
    assert sum(names == ("sctlr_el1", "SCTLR_EL1") for names in registers.calls) == 1


def test_isolation_reuses_pstate_and_hcr_from_other_passes() -> None:
    """A complete audit shares context evidence and appends portable isolation findings."""
    registers = Registers(
        {
            "pstate": 9,
            "ID_AA64PFR0_EL1": 0x1100,
            "ID_AA64MMFR1_EL1": (1 << 8) | (1 << 20),
            "HCR_EL2": (1 << 34) | (1 << 27),
            "SCTLR_EL2": 1,
        }
    )
    report = AArch64SecurityAuditor(registers).collect(MemoryReader(), target())
    isolation = {
        finding.title: finding for finding in report.findings if finding.category == "Isolation"
    }
    assert len(isolation) == 9
    assert "PAN=0" in isolation["PSTATE.PAN"].detail
    assert isolation["PSTATE.PAN"].severity is DiagnosticSeverity.WARNING
    assert "E2H=1" in isolation["EL2 configuration"].detail
    assert registers.calls.count(("pstate", "cpsr")) == 1
    assert registers.calls.count(("hcr_el2", "HCR_EL2")) == 1
    assert registers.calls.count(("sctlr_el2", "SCTLR_EL2")) == 1
    assert "secure configuration" in report.findings[0].detail


def test_protection_findings_share_registers_and_never_read_keys() -> None:
    """The complete audit reuses PSTATE and SCTLR for the new protection checks."""
    registers = Registers(
        {
            "pstate": 5,
            "ID_AA64ISAR1_EL1": 1 << 4,
            "ID_AA64PFR1_EL1": 1 | (2 << 8),
            "SCTLR_EL1": (1 << 31) | (1 << 43) | (1 << 40),
        }
    )
    report = AArch64SecurityAuditor(registers).collect(MemoryReader(), target())
    findings = {finding.title: finding for finding in report.findings}
    assert "EnIA=1" in findings["Address authentication controls"].detail
    assert "not global BTI enable bits" in findings["Branch target compatibility"].detail
    assert "TCF mode: synchronous" in findings["Tag checking configuration"].detail
    assert "PSTATE.TCO=0" in findings["Tag check override"].detail
    assert registers.calls.count(("pstate", "cpsr")) == 1
    assert registers.calls.count(("sctlr_el1", "SCTLR_EL1")) == 1
    assert not any("key" in name.lower() for names in registers.calls for name in names)
    assert all(finding.severity is not DiagnosticSeverity.PASS for finding in report.findings)


def test_secscan_physical_provider_is_explicit_and_refreshed(fake_gdb: object) -> None:
    """The real service receives physical access through the public session contract."""
    from pyGdbToolkit.target_memory import PhysicalMemoryRange, RestrictedPhysicalTableMemory

    fake_gdb._inferior = object()
    calls: list[int] = []
    provider_calls: list[int] = []

    def read(address: int, size: int) -> bytes:
        calls.append(address)
        return (0x400001 if address == 0x1000 else 0).to_bytes(size, "little")

    physical = RestrictedPhysicalTableMemory(
        "verified physical fixture",
        (PhysicalMemoryRange(0x1000, 0x3000),),
        read,
        lambda: True,
        True,
    )
    registers = Registers(
        {
            "CurrentEL": 4,
            "SCTLR_EL1": 1,
            "TCR_EL1": 39 | (39 << 16) | (2 << 30),
            "TTBR0_EL1": 0x1000,
            "TTBR1_EL1": 0x2000,
            "ID_AA64MMFR0_EL1": 0,
            "ID_AA64MMFR1_EL1": 0,
            "ID_AA64MMFR3_EL1": 0,
        }
    )

    class Runtime:
        def diagnose(
            self,
            reader: TargetMemory,
            service: DiagnosticServiceName,
            access: DiagnosticRuntimeAccess | None = None,
        ) -> DiagnosticResult:
            del reader
            assert access is not None
            return DEFAULT_DIAGNOSTIC_RUNTIME.diagnose(
                MemoryReader(),
                service,
                DiagnosticRuntimeAccess(
                    registers=registers,
                    physical_memory=access.physical_memory,
                ),
            )

    def provider():
        provider_calls.append(1)
        return physical

    session = ToolkitSession(diagnostic_runtime=Runtime())
    session.set_physical_table_access(provider)
    first = run_audit(session)
    assert any(
        finding.title == "Stage-1 W^X candidate" and finding.severity == "FAIL"
        for finding in first.findings
    )
    assert len(calls) == 32
    second = run_audit(session)
    assert len(provider_calls) == 2
    assert len(calls) == 64
    assert first.counts() == second.counts()
    session.set_physical_table_access(None)
    third = run_audit(session)
    assert len(calls) == 64
    assert "no explicitly configured" in third.findings[-1].detail
    assert SecscanReport.from_dict(first.to_dict()) == first
