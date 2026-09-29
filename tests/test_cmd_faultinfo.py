# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Tests for the architecture-neutral fault_info command adapter."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pyGdbToolkit.arch import (
    Architecture,
    DiagnosticPanel,
    DiagnosticReport,
    DiagnosticResult,
    DiagnosticRuntimeAccess,
    DiagnosticServiceName,
    DiagnosticTable,
    DiagnosticTableRow,
    TargetDescription,
)
from pyGdbToolkit.cmd_faultinfo import FaultInfoCmd, render_report, run_fault_analysis
from pyGdbToolkit.target_memory import TargetMemory


@dataclass
class Runtime:
    """Portable diagnostic-runtime test double."""

    result: DiagnosticResult
    services: list[DiagnosticServiceName]
    access: DiagnosticRuntimeAccess | None = None

    def diagnose(
        self,
        reader: TargetMemory,
        service: DiagnosticServiceName,
        access: DiagnosticRuntimeAccess | None = None,
    ) -> DiagnosticResult:
        """Record the generic access bundle and return the configured result."""
        del reader
        self.services.append(service)
        self.access = access
        return self.result


def _report() -> DiagnosticReport:
    """Create generic report data with no Arm-specific command dependency."""
    target = TargetDescription(Architecture.RISCV, "RISC-V", "RV32IM", "1.0")
    return DiagnosticReport(
        DiagnosticServiceName.FAULT_ANALYSIS,
        target,
        tables=(
            DiagnosticTable(
                "Portable Fault Overview",
                ("Property", "Value"),
                (DiagnosticTableRow(("Current PC", "0x00000010")),),
            ),
        ),
        panels=(DiagnosticPanel("Diagnostics & Probable Causes", ("• Portable cause.",)),),
    )


def test_run_fault_analysis_dispatches_generic_service_and_creates_neutral_adapters(
    fake_gdb: object,
) -> None:
    """The command requests fault analysis without inspecting architecture details."""
    fake_gdb._inferior = object()
    runtime = Runtime(DiagnosticResult.completed(_report()), [])

    report = run_fault_analysis(runtime)

    assert report.target.core_name == "RV32IM"
    assert runtime.services == [DiagnosticServiceName.FAULT_ANALYSIS]
    assert runtime.access is not None
    assert runtime.access.registers is not None
    assert runtime.access.symbols is not None


def test_command_rendering_preserves_generic_layout_titles(capsys: pytest.CaptureFixture[str]) -> None:
    """Generic report data renders through the established forced-terminal console."""
    render_report(_report())

    output = capsys.readouterr().out
    assert "Portable Fault Overview" in output
    assert "Current PC" in output
    assert "Diagnostics & Probable Causes" in output
    assert "Portable cause." in output


def test_run_fault_analysis_preserves_unsupported_and_access_errors(fake_gdb: object) -> None:
    """Unavailable portable results retain the legacy target error policy."""
    fake_gdb._inferior = object()
    unsupported = Runtime(DiagnosticResult.unavailable("unregistered"), [])
    access_error = Runtime(DiagnosticResult.unavailable("CPUID access denied", access_error=True), [])

    with pytest.raises(fake_gdb.GdbError, match="fault_info only supports ARM Cortex-M targets"):
        run_fault_analysis(unsupported)
    with pytest.raises(fake_gdb.GdbError, match="CPUID access denied"):
        run_fault_analysis(access_error)


def test_run_fault_analysis_preserves_no_inferior_error(fake_gdb: object) -> None:
    """Target-reader construction continues to report GDB's no-inferior failure."""
    runtime = Runtime(DiagnosticResult.completed(_report()), [])

    with pytest.raises(fake_gdb.GdbError, match="could not select inferior: no inferior selected"):
        run_fault_analysis(runtime)


def test_command_preserves_argument_error(fake_gdb: object) -> None:
    """The registered command continues to reject all command-line arguments."""
    with pytest.raises(fake_gdb.GdbError, match="fault_info does not accept arguments"):
        FaultInfoCmd().invoke("unexpected", False)
