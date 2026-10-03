"""Portable access-port profiles and architecture provider registry."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol, Sequence

from ..debug_port import AccessPort, DebugPortError, DebugPortTransport


@dataclass(frozen=True)
class AccessPortProfile:
    """Observed identity, capabilities and unavailable register evidence."""

    index: int
    architecture: str
    type_name: str
    identity: dict[str, int | str]
    capabilities: dict[str, bool | int | str | None]
    registers: dict[str, int]
    errors: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-serializable evidence without replacing unknowns with false."""
        return asdict(self)


class AccessPortProvider(Protocol):
    """Interpret AP registers for one architecture."""

    def supports(self, architecture: str) -> bool:
        """Check the GDB architecture name."""
        ...

    def profile(self, transport: DebugPortTransport, port: AccessPort) -> AccessPortProfile:
        """Collect a read-only profile."""
        ...


class AccessPortRegistry:
    """Dispatch AP profiling without architecture branches in commands."""

    def __init__(self, providers: Sequence[AccessPortProvider]) -> None:
        """Keep providers in priority order."""
        self._providers = tuple(providers)

    def provider(self, architecture: str) -> AccessPortProvider:
        """Resolve the provider for the selected inferior."""
        for provider in self._providers:
            if provider.supports(architecture):
                return provider
        raise DebugPortError(f"AP profiling is unsupported for architecture '{architecture}'")
