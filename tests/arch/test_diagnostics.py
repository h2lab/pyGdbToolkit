# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Tests for portable diagnostic-service dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from pyGdbToolkit.arch import (
    Architecture,
    ArchitectureDiagnosticRuntime,
    ArchitectureRegistry,
    DEFAULT_DIAGNOSTIC_RUNTIME,
    DiagnosticReport,
    DiagnosticResult,
    DiagnosticRuntimeAccess,
    DiagnosticSeverity,
    DiagnosticServiceName,
    DiagnosticServiceRegistry,
    ProbeResult,
    TargetDescription,
)
from pyGdbToolkit.arch.arm import CortexMSecurityAuditor
from pyGdbToolkit.arch.base import SystemRegisterSet
from pyGdbToolkit.target_memory import TargetMemory, TargetReadError


class FakeReader:
    """Target-memory fixture that rejects reads not expected by the test."""

    def read_bytes(self, address: int, size: int) -> bytes:
        """Reject byte reads."""
        raise AssertionError((address, size))

    def read_uint16(self, address: int) -> int:
        """Reject halfword reads."""
        raise AssertionError((address, 2))

    def read_uint32(self, address: int) -> int:
        """Reject word reads."""
        raise AssertionError((address, 4))


def test_diagnostic_severity_uses_lowercase_member_names() -> None:
    """Diagnostic severity serialization remains stable when derived with ``auto()``."""
    assert tuple(severity.value for severity in DiagnosticSeverity) == (
        "pass",
        "info",
        "warning",
        "error",
        "critical",
    )


class CortexMReader(FakeReader):
    """Reader fixture that exposes only a recognized Cortex-M CPUID."""

    def read_uint32(self, address: int) -> int:
        """Return the Cortex-M4 CPUID required by the default probe."""
        if address == 0xE000ED00:
            return 0x410FC240
        raise TargetReadError(address, 4, "not mapped")


class InaccessibleCortexMReader(FakeReader):
    """Reader fixture whose CPUID access fails."""

    def read_uint32(self, address: int) -> int:
        """Raise the production target read error for the CPUID request."""
        raise TargetReadError(address, 4, "access denied")


@dataclass
class FakeProbe:
    """Configurable architecture probe fixture."""

    architecture: Architecture
    result: ProbeResult

    def probe(self, reader: TargetMemory) -> ProbeResult:
        """Return the configured target-probe result."""
        del reader
        return self.result

    def read_system_registers(
        self,
        reader: TargetMemory,
        target: TargetDescription,
    ) -> SystemRegisterSet:
        """Provide the protocol method required by the architecture registry."""
        del reader
        return SystemRegisterSet(target.architecture, "test", ())


@dataclass
class FakeDiagnosticService:
    """Diagnostic collector fixture recording the dispatched target."""

    architecture: Architecture
    service: DiagnosticServiceName
    collected_targets: list[TargetDescription] = field(default_factory=list)

    def supports(self, target: TargetDescription) -> bool:
        """Accept every target used by this generic dispatch fixture."""
        del target
        return True

    def collect(
        self,
        reader: TargetMemory,
        target: TargetDescription,
        access: DiagnosticRuntimeAccess | None = None,
    ) -> DiagnosticReport:
        """Record dispatch and create an empty report."""
        del reader
        del access
        self.collected_targets.append(target)
        return DiagnosticReport(self.service, target)


def test_registry_dispatches_matching_architecture_service() -> None:
    """The service registered for the target architecture collects the report."""
    target = TargetDescription(Architecture.ARM, "Arm", "Cortex-M4", "r0p1")
    service = FakeDiagnosticService(Architecture.ARM, DiagnosticServiceName.SECURITY_AUDIT)
    registry = DiagnosticServiceRegistry((service,))

    result = registry.dispatch(FakeReader(), DiagnosticServiceName.SECURITY_AUDIT, target)

    assert result.is_available
    assert result.report == DiagnosticReport(DiagnosticServiceName.SECURITY_AUDIT, target)
    assert service.collected_targets == [target]


def test_registry_returns_explicit_unsupported_service_result() -> None:
    """A target receives an explicit result when no compatible service exists."""
    target = TargetDescription(Architecture.RISCV, "RISC-V", "RV32IM", "1.0")
    registry = DiagnosticServiceRegistry()

    result = registry.dispatch(FakeReader(), DiagnosticServiceName.FAULT_ANALYSIS, target)

    assert not result.is_available
    assert result.target == target
    assert result.unavailable_reason == (
        "diagnostic service 'fault-analysis' is not registered for architecture 'riscv'"
    )


def test_default_runtime_dispatches_registered_arm_security_service() -> None:
    """Default wiring dispatches the Arm security service for a Cortex-M target."""
    result = DEFAULT_DIAGNOSTIC_RUNTIME.diagnose(
        CortexMReader(),
        DiagnosticServiceName.SECURITY_AUDIT,
    )

    assert result.is_available
    assert result.target is not None
    assert result.target.architecture is Architecture.ARM
    assert result.report is not None
    assert result.report.service is DiagnosticServiceName.SECURITY_AUDIT
    assert result.report.target.core_name == "Cortex-M4"


def test_default_runtime_dispatches_registered_arm_fault_service() -> None:
    """Default wiring dispatches the Arm fault service through generic contracts."""
    result = DEFAULT_DIAGNOSTIC_RUNTIME.diagnose(
        CortexMReader(),
        DiagnosticServiceName.FAULT_ANALYSIS,
    )

    assert result.is_available
    assert result.report is not None
    assert result.report.service is DiagnosticServiceName.FAULT_ANALYSIS
    assert [table.title for table in result.report.tables] == [
        "ARM Cortex-M Fault Overview",
        "SCB (System Control Block) Status Registers",
    ]


def test_registered_arm_service_rejects_non_cortex_m_arm_target() -> None:
    """A Cortex-A description does not reach the Cortex-M-only collector."""
    target = TargetDescription(Architecture.ARM, "Arm", "Cortex-A53", "r0p4")
    registry = DiagnosticServiceRegistry((CortexMSecurityAuditor(),))

    result = registry.dispatch(FakeReader(), DiagnosticServiceName.SECURITY_AUDIT, target)

    assert result == DiagnosticResult.unavailable(
        "diagnostic service 'security-audit' does not support target 'Cortex-A53'",
        target,
    )


def test_default_runtime_preserves_cortex_m_cpuid_access_error() -> None:
    """An inaccessible CPUID remains a diagnostic access failure, not unsupported hardware."""
    result = DEFAULT_DIAGNOSTIC_RUNTIME.diagnose(
        InaccessibleCortexMReader(),
        DiagnosticServiceName.SECURITY_AUDIT,
    )

    assert result == DiagnosticResult.unavailable(
        "could not read Cortex-M CPUID: " "could not read 4 byte(s) at 0xE000ED00: access denied",
        access_error=True,
    )


def test_runtime_returns_probe_reason_before_dispatching_service() -> None:
    """An unsupported target never reaches a registered diagnostic collector."""
    service = FakeDiagnosticService(Architecture.ARM, DiagnosticServiceName.FAULT_ANALYSIS)
    runtime = ArchitectureDiagnosticRuntime(
        ArchitectureRegistry(
            (FakeProbe(Architecture.ARM, ProbeResult.unavailable("CPUID is unavailable")),)
        ),
        DiagnosticServiceRegistry((service,)),
    )

    result = runtime.diagnose(FakeReader(), DiagnosticServiceName.FAULT_ANALYSIS)

    assert result == DiagnosticResult.unavailable(
        "no registered architecture probe recognized the target"
    )
    assert service.collected_targets == []


def test_registry_rejects_duplicate_service_registration() -> None:
    """Service dispatch remains deterministic for a service and architecture pair."""
    service = FakeDiagnosticService(Architecture.ARM, DiagnosticServiceName.FAULT_ANALYSIS)
    registry = DiagnosticServiceRegistry((service,))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            FakeDiagnosticService(Architecture.ARM, DiagnosticServiceName.FAULT_ANALYSIS)
        )
