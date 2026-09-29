# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Tests for the architecture-neutral secscan command adapter."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pyGdbToolkit.arch import (
    Architecture,
    DiagnosticField,
    DiagnosticFinding,
    DiagnosticReport,
    DiagnosticResult,
    DiagnosticRuntimeAccess,
    DiagnosticSection,
    DiagnosticServiceName,
    DiagnosticSeverity,
    TargetDescription,
)
from pyGdbToolkit.cmd_secscan import SecscanFinding, SecscanReport, generate_html_report, run_audit
from pyGdbToolkit.target_memory import TargetMemory


@dataclass
class Runtime:
    """Portable diagnostic-runtime test double."""

    result: DiagnosticResult
    services: list[DiagnosticServiceName]

    def diagnose(
        self,
        reader: TargetMemory,
        service: DiagnosticServiceName,
        access: DiagnosticRuntimeAccess | None = None,
    ) -> DiagnosticResult:
        """Return the configured portable result."""
        del reader
        del access
        self.services.append(service)
        return self.result


def _runtime() -> Runtime:
    """Create a runtime returning a portable report without an Arm dependency."""
    target = TargetDescription(Architecture.RISCV, "RISC-V", "RV32IM", "1.0")
    report = DiagnosticReport(
        DiagnosticServiceName.SECURITY_AUDIT,
        target,
        (
            DiagnosticSection(
                "Target",
                (DiagnosticField("vendor", "Example"), DiagnosticField("device_name", "Board")),
            ),
        ),
        (
            DiagnosticFinding(
                "Protection",
                DiagnosticSeverity.WARNING,
                "Example finding",
                "Portable diagnostic report.",
            ),
        ),
    )
    return Runtime(DiagnosticResult.completed(report), [])


def test_run_audit_adapts_portable_report_without_architecture_branches(fake_gdb: object) -> None:
    """The command preserves its schema while accepting a non-Arm diagnostic result."""
    fake_gdb._inferior = object()
    runtime = _runtime()

    report = run_audit(runtime)

    assert runtime.services == [DiagnosticServiceName.SECURITY_AUDIT]
    assert report.core == "RV32IM"
    assert report.vendor == "Example"
    assert report.device_name == "Board"
    assert report.findings[0].severity == "WARN"
    assert report.to_dict()["summary"] == {"FAIL": 0, "WARN": 1, "INFO": 0, "PASS": 0}


def test_run_audit_preserves_unsupported_target_error(fake_gdb: object) -> None:
    """Unavailable portable results retain the established user-facing error."""
    fake_gdb._inferior = object()
    runtime = Runtime(DiagnosticResult.unavailable("not registered"), [])

    with pytest.raises(fake_gdb.GdbError, match="secscan only supports ARM Cortex-M targets"):
        run_audit(runtime)


def test_run_audit_preserves_target_reader_construction_error(fake_gdb: object) -> None:
    """Reader construction failures retain their detailed target-access error."""
    with pytest.raises(fake_gdb.GdbError, match="could not select inferior: no inferior selected"):
        run_audit(_runtime())


def test_run_audit_preserves_probe_access_error(fake_gdb: object) -> None:
    """A probe access error is rendered as its detail instead of unsupported hardware."""
    fake_gdb._inferior = object()
    runtime = Runtime(
        DiagnosticResult.unavailable(
            "could not read Cortex-M CPUID: access denied", access_error=True
        ),
        [],
    )

    with pytest.raises(fake_gdb.GdbError, match="could not read Cortex-M CPUID: access denied"):
        run_audit(runtime)


def test_portable_adapter_preserves_json_schema_and_html_escaping() -> None:
    """The unchanged report formats remain deterministic after portable dispatch."""
    report = SecscanReport(
        "Cortex-M33",
        "Vendor & Co.",
        "Board <A>",
        "2026-09-28T00:00:00+00:00",
        (SecscanFinding("MPU", "PASS", "W^X <enabled>", "Safe & sound."),),
    )

    assert list(report.to_dict()) == [
        "core",
        "vendor",
        "device_name",
        "generated_at",
        "summary",
        "findings",
    ]
    html = generate_html_report(report)
    assert "Board &lt;A&gt;" in html
    assert "W^X &lt;enabled&gt;" in html
    assert "Safe &amp; sound." in html
    assert '<span class="badge pass">PASS 1</span>' in html
