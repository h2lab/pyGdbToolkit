# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Composition of portable access-port contracts and architecture providers."""

from typing import Callable

from .arch.dap import AccessPortRegistry
from .arch.arm.dap import ArmAccessPortProvider
from .debug_port import (
    AccessPort,
    DebugPortError,
    DebugPortTransport,
    OpenOcdMonitorTransport,
    PyOcdMonitorTransport,
)
from .ocd import OCD, OcdDetector, OcdIdentifier, OcdInfo

DEFAULT_AP_REGISTRY = AccessPortRegistry((ArmAccessPortProvider(),))


class AutoDebugPortTransport:
    """Choose the OCD transport transparently for the current GDB connection."""

    def __init__(self, execute: Callable[[str], str], detector: OcdDetector = OCD) -> None:
        """Share the toolkit's detected identity and inject monitor execution."""
        self._execute = execute
        self._detector = detector
        self._info: OcdInfo | None = None
        self._transport: DebugPortTransport | None = None

    def _backend(self) -> DebugPortTransport:
        info = self._detector.get()
        if info.identifier == OcdIdentifier.UNKNOWN:
            self._transport = None
            self._info = None
            raise DebugPortError(f"No supported OCD detected: {info.evidence}")
        if info is not self._info or self._transport is None:
            if info.identifier == OcdIdentifier.OPENOCD:
                self._transport = OpenOcdMonitorTransport(self._execute)
            else:
                self._transport = PyOcdMonitorTransport(self._execute)
            self._info = info
        return self._transport

    @property
    def backend_name(self) -> str:
        """Expose the concrete transport in reports, rather than the proxy name."""
        return type(self._backend()).__name__

    @property
    def discovery(self) -> str:
        """Expose the backend-specific discovery method in reports."""
        backend = self._backend()
        if isinstance(backend, OpenOcdMonitorTransport):
            return backend.discovery
        return "debug-server inventory"

    def list_access_ports(self) -> tuple[AccessPort, ...]:
        """List ports through the currently detected server."""
        return self._backend().list_access_ports()

    def select_access_port(self, index: int) -> None:
        """Select a port through the currently detected server."""
        self._backend().select_access_port(index)

    def read_ap(self, index: int, address: int) -> int:
        """Read an AP register through the currently detected server."""
        return self._backend().read_ap(index, address)
