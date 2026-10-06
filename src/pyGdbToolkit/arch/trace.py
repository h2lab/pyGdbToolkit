"""Architecture-neutral, read-only trace capability descriptions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Callable, Mapping

from ..target_memory import TargetMemory
from .base import Architecture, RegisterValue, TargetDescription


class TraceComponentKind(StrEnum):
    """Trace sources and embedded sinks recognized by the toolkit."""

    ETM = "etm"
    ETB = "etb"
    MTB = "mtb"
    ETF = "etf"


@dataclass(frozen=True)
class TraceComponent:
    """One detected component; unavailable optional capabilities remain None."""

    kind: TraceComponentKind
    base: int
    version: str | None = None
    security_filtering: bool | None = None
    secure_exception_levels: int | None = None
    nonsecure_exception_levels: int | None = None
    buffer_size_bytes: int | None = None
    registers: tuple[RegisterValue, ...] = ()


@dataclass(frozen=True)
class TraceCapabilities:
    """Confirmed trace components, not trace enablement or access permissions."""

    components: tuple[TraceComponent, ...] = ()
    unavailable_reason: str | None = None

    @property
    def is_available(self) -> bool:
        """Whether the architecture could inspect its trace topology."""
        return self.unavailable_reason is None

    def has_etm(self) -> bool:
        """Whether at least one ETM was positively identified."""
        return any(component.kind == TraceComponentKind.ETM for component in self.components)

    def has_etb(self) -> bool:
        """Whether at least one embedded trace buffer was identified."""
        return any(component.kind == TraceComponentKind.ETB for component in self.components)

    def has_mtb(self) -> bool:
        """Whether at least one micro trace buffer was identified."""
        return any(component.kind == TraceComponentKind.MTB for component in self.components)

    def has_etf(self) -> bool:
        """Whether at least one embedded trace FIFO was identified."""
        return any(component.kind == TraceComponentKind.ETF for component in self.components)


TraceCapabilityProbe = Callable[[TargetMemory, TargetDescription], TraceCapabilities]


class TraceCapabilityRegistry:
    """Dispatch read-only capability probes without architectural assumptions."""

    def __init__(self, probes: Mapping[Architecture, TraceCapabilityProbe]) -> None:
        """Register one capability probe per supported architecture."""
        self._probes = dict(probes)

    def inspect(self, reader: TargetMemory, target: TargetDescription) -> TraceCapabilities:
        """Return trace evidence or an explicit unsupported-architecture result."""
        probe = self._probes.get(target.architecture)
        if probe is None:
            return TraceCapabilities(
                unavailable_reason=f"trace probing is not supported for {target.architecture}"
            )
        return probe(reader, target)
