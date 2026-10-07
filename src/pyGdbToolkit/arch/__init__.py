# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Portable architecture detection, inspection, and diagnostic APIs."""

from .aarch64.probe import DEFAULT_AARCH64_PROBES
from .aarch64.security import AArch64SecurityAuditor
from .arm.probe import DEFAULT_ARM_PROBES
from .arm.fault import CortexMFaultCollector
from .arm.security import CortexMSecurityAuditor
from .base import (
    Architecture,
    ArchitectureProbe,
    ProbeResult,
    RegisterValue,
    SystemRegisterSet,
    TargetDescription,
)
from .diagnostics import (
    ArchitectureDiagnosticRuntime,
    DiagnosticField,
    DiagnosticFinding,
    DiagnosticContent,
    DiagnosticPanel,
    DiagnosticReport,
    DiagnosticRegisterReader,
    DiagnosticResult,
    DiagnosticRuntimeAccess,
    DiagnosticRuntime,
    DiagnosticSection,
    DiagnosticService,
    DiagnosticServiceName,
    DiagnosticServiceRegistry,
    DiagnosticSeverity,
    DiagnosticSymbolResolver,
    DiagnosticTable,
    DiagnosticTableRow,
    DiagnosticValue,
)
from .registry import ArchitectureRegistry

DEFAULT_ARCHITECTURE_REGISTRY = ArchitectureRegistry((*DEFAULT_AARCH64_PROBES, *DEFAULT_ARM_PROBES))
DEFAULT_DIAGNOSTIC_SERVICE_REGISTRY = DiagnosticServiceRegistry(
    (CortexMSecurityAuditor(), CortexMFaultCollector(), AArch64SecurityAuditor())
)
DEFAULT_DIAGNOSTIC_RUNTIME: DiagnosticRuntime = ArchitectureDiagnosticRuntime(
    DEFAULT_ARCHITECTURE_REGISTRY,
    DEFAULT_DIAGNOSTIC_SERVICE_REGISTRY,
)

__all__ = [
    "Architecture",
    "ArchitectureDiagnosticRuntime",
    "ArchitectureProbe",
    "ArchitectureRegistry",
    "DEFAULT_ARCHITECTURE_REGISTRY",
    "DEFAULT_DIAGNOSTIC_RUNTIME",
    "DEFAULT_DIAGNOSTIC_SERVICE_REGISTRY",
    "DiagnosticField",
    "DiagnosticFinding",
    "DiagnosticContent",
    "DiagnosticPanel",
    "DiagnosticReport",
    "DiagnosticRegisterReader",
    "DiagnosticResult",
    "DiagnosticRuntimeAccess",
    "DiagnosticRuntime",
    "DiagnosticSection",
    "DiagnosticService",
    "DiagnosticServiceName",
    "DiagnosticServiceRegistry",
    "DiagnosticSeverity",
    "DiagnosticSymbolResolver",
    "DiagnosticTable",
    "DiagnosticTableRow",
    "DiagnosticValue",
    "ProbeResult",
    "RegisterValue",
    "SystemRegisterSet",
    "TargetDescription",
]
