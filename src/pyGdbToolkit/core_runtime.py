# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Backend-neutral CPU-context orchestration shared by DAP commands and RPC."""

from __future__ import annotations

import gdb

from .ocd import OCD, OcdDetector
from .ocd.base import CoreBackend, CoreInfo, CoreSessionState
from .ocd.context import GdbCoreContext
from .ocd.jlinkgdbserver import JLinkClusterState, JLinkCoreState
from .ocd.registry import CoreBackendRegistry
from .session import SESSION, ToolkitSession

__all__ = [
    "CORES",
    "CoreInfo",
    "CoreSessionState",
    "GdbCoreController",
    "JLinkClusterState",
    "JLinkCoreState",
]


class GdbCoreController:
    """Route common CPU operations through a uniform debug-server backend interface."""

    def __init__(self, session: ToolkitSession = SESSION, detector: OcdDetector = OCD) -> None:
        """Bind session, detection and shared GDB lifecycle services to backend strategies."""
        self.session = session
        self.detector = detector
        self.context = GdbCoreContext(session)
        self._backends = CoreBackendRegistry(self.context)

    def _backend(self) -> CoreBackend:
        self.context.connection()
        return self._backends.resolve(self.detector.get().identifier)

    def register_jlink_cluster(
        self, endpoints: dict[int, str], initial_core: int, devices: dict[int, str] | None = None
    ) -> None:
        """Preserve the supervisor API while delegating configured-cluster registration."""
        self._backend().register_cluster(endpoints, initial_core, devices)

    def register_jlink_core(self, name: str) -> None:
        """Preserve the supervisor API while delegating attached-core registration."""
        self._backend().register_attached_core(name)

    def list(self) -> tuple[CoreInfo, ...]:
        """List backend CPU contexts without attaching other endpoints."""
        return self._backend().list_cores()

    def current(self) -> CoreInfo:
        """Return the backend CPU corresponding to the selected GDB context."""
        return self._backend().current_core()

    def select(self, core_id: int) -> CoreInfo:
        """Validate a request, delegate selection, verify the result and invalidate caches."""
        if isinstance(core_id, bool) or not isinstance(core_id, int) or core_id < 0:
            raise gdb.GdbError("Core ID must be a non-negative integer")
        backend = self._backend()
        core = next((core for core in backend.list_cores() if core.id == core_id), None)
        if core is None:
            raise gdb.GdbError(f"Core {core_id} not discovered; use 'dap core list'")
        invalidates = backend.invalidates_selection
        try:
            backend.select_core(core)
            selected = backend.current_core()
            if selected.id != core_id:
                raise gdb.GdbError(f"GDB did not select core {core_id}")
            return selected
        finally:
            if invalidates:
                self.session.invalidate()


CORES = GdbCoreController()
