# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Common CPU-context models and debug-server backend contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol

import gdb

from ..session import SessionSlice, ToolkitSession
from .detection import OcdIdentifier


@dataclass(frozen=True)
class CoreInfo:
    """A CPU context with explicit inventory scope and identity evidence."""

    id: int
    name: str
    selected: bool
    endpoint: str
    inferior: int | None = None
    thread: int | None = None
    scope: str = "physical-core-inventory"
    identity_source: str = "debug-server"

    def to_dict(self) -> dict[str, Any]:
        """Return stable command and RPC metadata."""
        return asdict(self)


@dataclass
class CoreSessionState(SessionSlice):
    """Track reusable per-core GDB attachments and connection parameters."""

    host: str = ""
    port: int = 0
    connection_type: str = ""
    attachments: dict[int, tuple[int, int]] = field(default_factory=dict)

    def reset(self) -> None:
        """Discard connection identities without disconnecting GDB."""
        self.host = ""
        self.port = 0
        self.connection_type = ""
        self.attachments.clear()


class CoreContextAccess(Protocol):
    """Portable GDB lifecycle operations available to backend strategies."""

    session: ToolkitSession

    def connection(self) -> Any:
        """Return the active remote connection or fail explicitly."""
        ...

    def attached(self, state: CoreSessionState) -> dict[int, Any]:
        """Resolve still-valid per-core inferior attachments."""
        ...

    def select_endpoint(
        self, core: CoreInfo, state: CoreSessionState, verify: Callable[[int], None] | None = None
    ) -> None:
        """Attach or reuse an endpoint, restoring the prior context on attachment failure."""
        ...


class CoreBackend(ABC):
    """Uniform external interface for debug-server CPU context strategies."""

    identifier: OcdIdentifier

    def __init__(self, context: CoreContextAccess) -> None:
        """Bind a backend to shared GDB lifecycle and session services."""
        self.context = context

    @property
    def invalidates_selection(self) -> bool:
        """Whether selection attempts invalidate cached target inspection."""
        return True

    @abstractmethod
    def list_cores(self) -> tuple[CoreInfo, ...]:
        """List contexts without attaching additional endpoints."""

    def current_core(self) -> CoreInfo:
        """Return the CPU context currently selected in GDB."""
        for core in self.list_cores():
            if core.selected:
                return core
        raise gdb.GdbError("No physical CPU corresponds to the selected GDB context")

    @abstractmethod
    def select_core(self, core: CoreInfo) -> None:
        """Select one listed context using backend-specific evidence."""

    def register_attached_core(self, name: str) -> None:
        """Reject registration when a backend does not support attached-core metadata."""
        del name
        raise gdb.GdbError("Attached-core registration requires J-Link")

    def register_cluster(
        self, endpoints: dict[int, str], initial_core: int, devices: dict[int, str] | None = None
    ) -> None:
        """Reject configured clusters when the backend supplies its own inventory."""
        del endpoints, initial_core, devices
        raise gdb.GdbError("Cluster registration requires J-Link")
