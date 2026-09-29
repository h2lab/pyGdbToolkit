# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Portable diagnostic-service contracts, reports, and runtime dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto, unique
from typing import Protocol, TypeAlias

from ..target_memory import TargetMemory
from .base import Architecture, TargetDescription
from .registry import ArchitectureRegistry

DiagnosticValue: TypeAlias = bool | int | str | None


@unique
class DiagnosticServiceName(StrEnum):
    """Architecture-neutral diagnostics offered by command entry points."""

    SECURITY_AUDIT = "security-audit"
    FAULT_ANALYSIS = "fault-analysis"


@unique
class DiagnosticSeverity(StrEnum):
    """Common severity levels used by diagnostic findings."""

    PASS = auto()
    INFO = auto()
    WARNING = auto()
    ERROR = auto()
    CRITICAL = auto()


@dataclass(frozen=True)
class DiagnosticField:
    """One named, scalar diagnostic value suitable for a renderer."""

    name: str
    value: DiagnosticValue


@dataclass(frozen=True)
class DiagnosticTableRow:
    """One renderer-independent row in a diagnostic table."""

    values: tuple[DiagnosticValue, ...]


@dataclass(frozen=True)
class DiagnosticTable:
    """One renderer-independent table in a diagnostic report."""

    title: str
    columns: tuple[str, ...]
    rows: tuple[DiagnosticTableRow, ...]

    def __post_init__(self) -> None:
        """Ensure every row matches the declared table column count."""
        if not self.columns:
            raise ValueError("a diagnostic table must have at least one column")
        if any(len(row.values) != len(self.columns) for row in self.rows):
            raise ValueError("diagnostic table rows must match the column count")


@dataclass(frozen=True)
class DiagnosticPanel:
    """One renderer-independent, ordered group of diagnostic lines."""

    title: str
    lines: tuple[str, ...]


DiagnosticContent: TypeAlias = DiagnosticTable | DiagnosticPanel


@dataclass(frozen=True)
class DiagnosticFinding:
    """One portable diagnostic observation."""

    category: str
    severity: DiagnosticSeverity
    title: str
    detail: str


@dataclass(frozen=True)
class DiagnosticSection:
    """One renderer-independent grouping of diagnostic values."""

    title: str
    fields: tuple[DiagnosticField, ...]


@dataclass(frozen=True)
class DiagnosticReport:
    """A complete architecture-specific diagnostic report."""

    service: DiagnosticServiceName
    target: TargetDescription
    sections: tuple[DiagnosticSection, ...] = ()
    findings: tuple[DiagnosticFinding, ...] = ()
    tables: tuple[DiagnosticTable, ...] = ()
    panels: tuple[DiagnosticPanel, ...] = ()
    blocks: tuple[DiagnosticContent, ...] = ()


@dataclass(frozen=True)
class DiagnosticResult:
    """A completed report or a reason the requested diagnostic is unavailable."""

    report: DiagnosticReport | None = None
    target: TargetDescription | None = None
    unavailable_reason: str | None = None
    access_error: bool = False

    def __post_init__(self) -> None:
        """Require exactly one completed or unavailable result state."""
        if (self.report is None) == (self.unavailable_reason is None):
            raise ValueError("a diagnostic result must contain a report or an unavailable reason")
        if self.report is not None and self.target != self.report.target:
            raise ValueError("a completed diagnostic result must retain its report target")
        if self.report is not None and self.access_error:
            raise ValueError("a completed diagnostic result cannot contain an access error")

    @classmethod
    def completed(cls, report: DiagnosticReport) -> DiagnosticResult:
        """Create a completed diagnostic result."""
        return cls(report=report, target=report.target)

    @classmethod
    def unavailable(
        cls,
        reason: str,
        target: TargetDescription | None = None,
        *,
        access_error: bool = False,
    ) -> DiagnosticResult:
        """Create an explicitly unavailable diagnostic result."""
        return cls(target=target, unavailable_reason=reason, access_error=access_error)

    @property
    def is_available(self) -> bool:
        """Whether the requested diagnostic completed."""
        return self.report is not None


class DiagnosticService(Protocol):
    """An architecture-specific collector behind a portable diagnostic service."""

    architecture: Architecture
    service: DiagnosticServiceName

    def supports(self, target: TargetDescription) -> bool:
        """Return whether the service can collect this target description."""
        ...

    def collect(
        self,
        reader: TargetMemory,
        target: TargetDescription,
        access: DiagnosticRuntimeAccess | None = None,
    ) -> DiagnosticReport:
        """Collect one report for a compatible target."""
        ...


class DiagnosticRegisterReader(Protocol):
    """Read a runtime register from any of a service-provided set of names."""

    def read_first(self, names: tuple[str, ...]) -> int | None:
        """Return the first available named register, or ``None``."""
        ...


class DiagnosticSymbolResolver(Protocol):
    """Resolve a runtime address for architecture-neutral report renderers."""

    def resolve(self, address: int) -> str:
        """Return a symbol description or ``"?"`` when no symbol is available."""
        ...


@dataclass(frozen=True)
class DiagnosticRuntimeAccess:
    """Optional neutral runtime facilities needed by a diagnostic collector."""

    registers: DiagnosticRegisterReader | None = None
    symbols: DiagnosticSymbolResolver | None = None


class DiagnosticRuntime(Protocol):
    """Portable runtime entry point used by architecture-neutral commands."""

    def diagnose(
        self,
        reader: TargetMemory,
        service: DiagnosticServiceName,
        access: DiagnosticRuntimeAccess | None = None,
    ) -> DiagnosticResult:
        """Identify the target and run one requested diagnostic service."""
        ...


class DiagnosticServiceRegistry:
    """Resolve diagnostic collectors by requested service and target architecture."""

    def __init__(self, services: tuple[DiagnosticService, ...] = ()) -> None:
        """Create a registry and register its initial service implementations."""
        self._services: dict[tuple[Architecture, DiagnosticServiceName], DiagnosticService] = {}
        for service in services:
            self.register(service)

    def register(self, service: DiagnosticService) -> None:
        """Register one service implementation.

        Raises
        ------
        ValueError
            If a service is already registered for the same architecture and name.
        """
        key = (service.architecture, service.service)
        if key in self._services:
            raise ValueError(
                f"diagnostic service '{service.service}' is already registered "
                f"for architecture '{service.architecture}'"
            )
        self._services[key] = service

    def resolve(
        self,
        service: DiagnosticServiceName,
        target: TargetDescription,
    ) -> DiagnosticService | None:
        """Return the registered service implementation compatible with a target."""
        implementation = self._services.get((target.architecture, service))
        if implementation is None or not implementation.supports(target):
            return None
        return implementation

    def dispatch(
        self,
        reader: TargetMemory,
        service: DiagnosticServiceName,
        target: TargetDescription,
        access: DiagnosticRuntimeAccess | None = None,
    ) -> DiagnosticResult:
        """Run a compatible service or return an explicit unsupported result."""
        implementation = self._services.get((target.architecture, service))
        if implementation is None:
            return DiagnosticResult.unavailable(
                (
                    f"diagnostic service '{service}' is not registered "
                    f"for architecture '{target.architecture}'"
                ),
                target,
            )
        if not implementation.supports(target):
            return DiagnosticResult.unavailable(
                f"diagnostic service '{service}' does not support target '{target.core_name}'",
                target,
            )

        report = implementation.collect(reader, target, access)
        if report.service is not service:
            raise ValueError("diagnostic service returned a report for a different service")
        if report.target != target:
            raise ValueError("diagnostic service returned a report for a different target")
        return DiagnosticResult.completed(report)


class ArchitectureDiagnosticRuntime:
    """Adapt architecture probing and service dispatch to the diagnostic runtime protocol."""

    def __init__(
        self,
        architecture_registry: ArchitectureRegistry,
        service_registry: DiagnosticServiceRegistry,
    ) -> None:
        """Create a runtime using the supplied architecture and service registries."""
        self._architecture_registry = architecture_registry
        self._service_registry = service_registry

    def diagnose(
        self,
        reader: TargetMemory,
        service: DiagnosticServiceName,
        access: DiagnosticRuntimeAccess | None = None,
    ) -> DiagnosticResult:
        """Identify the target, then dispatch its implementation of a diagnostic."""
        probe_result = self._architecture_registry.probe(reader)
        if probe_result.target is None:
            return DiagnosticResult.unavailable(
                probe_result.unavailable_reason or "target unavailable",
                access_error=probe_result.access_error,
            )
        return self._service_registry.dispatch(reader, service, probe_result.target, access)
