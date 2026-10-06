# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Resolve debug-server strategies through one shared backend interface."""

from __future__ import annotations

import gdb

from .base import CoreBackend, CoreContextAccess
from .detection import OcdIdentifier
from .jlinkgdbserver import JLinkCoreBackend
from .openocd import OpenOcdCoreBackend
from .pyocd import PyOcdCoreBackend


class CoreBackendRegistry:
    """Own backend strategies bound to a controller's GDB context services."""

    def __init__(self, context: CoreContextAccess) -> None:
        """Instantiate the supported strategies once per controller."""
        self._backends: dict[OcdIdentifier, CoreBackend] = {
            backend.identifier: backend
            for backend in (
                JLinkCoreBackend(context),
                OpenOcdCoreBackend(context),
                PyOcdCoreBackend(context),
            )
        }

    def resolve(self, identifier: OcdIdentifier) -> CoreBackend:
        """Resolve a detected backend without falling back to a different server."""
        backend = self._backends.get(identifier)
        if backend is None:
            raise gdb.GdbError("Core selection requires a recognized debug server")
        return backend
