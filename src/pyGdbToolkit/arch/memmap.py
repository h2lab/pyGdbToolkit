# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Portable memory-map evidence and architecture-provider contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, Sequence

from ..target_memory import TargetMemory


@dataclass(frozen=True)
class MemoryRegion:
    """A declared half-open range, not proof that its contents are accessible."""

    start: int
    end: int
    kind: str
    source: str
    name: str = ""
    probe_allowed: bool = False
    protection: str = "unknown"
    evidence: str = ""

    def __post_init__(self) -> None:
        """Reject empty ranges and addresses beyond the portable 64-bit domain."""
        if not 0 <= self.start < self.end <= 1 << 64:
            raise ValueError("memory region must satisfy 0 <= start < end <= 2**64")

    def to_dict(self) -> dict[str, Any]:
        """Serialize declared metadata independently of probe observations."""
        return dict(asdict(self), size=self.end - self.start)


@dataclass(frozen=True)
class MemoryBaseline:
    """A family-specific reference window, never discovered target memory."""

    vendor: str
    family: str
    architecture: str
    start: int
    end: int
    kind: str
    reference: str
    note: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate reference ranges without assigning target-access permissions."""
        if not 0 <= self.start < self.end <= 1 << 64:
            raise ValueError("memory baseline must satisfy 0 <= start < end <= 2**64")

    def to_dict(self) -> dict[str, Any]:
        """Export candidate windows without presenting their span as capacity."""
        return asdict(self)


@dataclass(frozen=True)
class TargetFingerprint:
    """Device identity with explicit provenance and hardware confirmation."""

    vendor: str
    soc: str
    architecture: str
    confidence: str
    evidence: tuple[str, ...]
    server_soc: str | None = None

    @property
    def confirmed(self) -> bool:
        """Whether an unambiguous hardware identity backs this fingerprint."""
        return self.confidence == "hardware-confirmed"

    def to_dict(self) -> dict[str, Any]:
        """Export only identity evidence, never device serial numbers."""
        return asdict(self)


@dataclass(frozen=True)
class ExecutionHint:
    """A runtime address and role, never a physical range or memory technology."""

    name: str
    address: int
    role: str
    evidence: str


@dataclass
class RuntimeMemoryEvidence:
    """Architecture-owned runtime clues and independently documented regions."""

    hints: list[ExecutionHint] = field(default_factory=list)
    regions: list[MemoryRegion] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    candidates: list[MemoryBaseline] = field(default_factory=list)


class MemoryMapProvider(Protocol):
    """Supply architecture-specific interpretation without target memory reads."""

    def supports(self, architecture: str) -> bool:
        """Accept a GDB architecture name."""
        ...

    def describe(self, start: int, end: int) -> str:
        """Describe architectural address space, never physical memory type."""
        ...

    def fingerprint(
        self, memory: TargetMemory, architecture: str, server: TargetFingerprint | None
    ) -> TargetFingerprint | None:
        """Confirm identity using documented architectural metadata reads only."""
        ...

    def runtime_evidence(
        self,
        memory: TargetMemory,
        registers: dict[str, int],
        fingerprint: TargetFingerprint | None,
        regions: Sequence[MemoryRegion],
    ) -> RuntimeMemoryEvidence:
        """Collect bounded execution clues without inferring physical memory capacity."""
        ...


class GenericMemoryMapProvider:
    """Retain unknown address spaces on unsupported architectures."""

    def supports(self, architecture: str) -> bool:
        """Accept any architecture as a fallback."""
        return True

    def describe(self, start: int, end: int) -> str:
        """Avoid inferring memory type from addresses."""
        return "unknown address space"

    def fingerprint(
        self, memory: TargetMemory, architecture: str, server: TargetFingerprint | None
    ) -> TargetFingerprint | None:
        """Preserve server evidence without fabricating hardware confirmation."""
        return server

    def runtime_evidence(
        self,
        memory: TargetMemory,
        registers: dict[str, int],
        fingerprint: TargetFingerprint | None,
        regions: Sequence[MemoryRegion],
    ) -> RuntimeMemoryEvidence:
        """Expose PC/SP roles on any architecture without guessing memory types."""
        result = RuntimeMemoryEvidence()
        for name, role in (("pc", "code"), ("sp", "stack")):
            address = registers.get(name)
            if address is not None and 0 <= address < 1 << 64:
                result.hints.append(
                    ExecutionHint(
                        name.upper(),
                        address,
                        role,
                        "Selected GDB frame register; physical base and extent unknown",
                    )
                )
        return result


class MemoryMapRegistry:
    """Resolve architecture-specific providers in priority order."""

    def __init__(
        self,
        providers: Sequence[MemoryMapProvider] = (),
        baselines: Sequence[MemoryBaseline] = (),
    ) -> None:
        """Install a generic fallback after explicitly registered providers."""
        self._providers = (*providers, GenericMemoryMapProvider())
        self._baselines = tuple(baselines)

    def provider(self, architecture: str) -> MemoryMapProvider:
        """Resolve the first provider accepting the selected architecture."""
        return next(provider for provider in self._providers if provider.supports(architecture))

    def baselines(self, vendor: str | None = None) -> tuple[MemoryBaseline, ...]:
        """List family reference windows, optionally filtered by manufacturer."""
        if vendor is None:
            return self._baselines
        token = "".join(character for character in vendor.casefold() if character.isalnum())
        matches = tuple(
            baseline
            for baseline in self._baselines
            if any(
                token == "".join(character for character in name.casefold() if character.isalnum())
                for name in (baseline.vendor, *baseline.aliases)
            )
        )
        if not matches:
            vendors = ", ".join(sorted({baseline.vendor for baseline in self._baselines}))
            raise ValueError(
                f"Unknown memory baseline manufacturer '{vendor}'; available: {vendors or 'none'}"
            )
        return matches
