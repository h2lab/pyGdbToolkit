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
    assert registers.calls == [("midr_el1", "MIDR_EL1"), ("currentel", "CurrentEL")]


def test_missing_registers_do_not_invent_cpu_version_or_security() -> None:
    """A report remains available without identifying a CPU or declaring protection."""
    report = AArch64SecurityAuditor().collect(MemoryReader(), target())
    assert len(report.findings) == 4
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
    assert report.counts() == {"FAIL": 0, "WARN": 0, "INFO": 4, "PASS": 0}
    assert SecscanReport.from_dict(report.to_dict()) == report
