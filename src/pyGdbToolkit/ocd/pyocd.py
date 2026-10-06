# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""pyOCD CPU inventory and per-core endpoint strategy."""

from __future__ import annotations

import re

import gdb

from .base import CoreBackend, CoreInfo, CoreSessionState
from .detection import OcdIdentifier


class PyOcdCoreBackend(CoreBackend):
    """Discover pyOCD CPUs and select their independently exposed GDB endpoints."""

    identifier = OcdIdentifier.PYOCD

    def _state(self) -> CoreSessionState:
        state = self.context.session.state(CoreSessionState)
        inferior = gdb.selected_inferior()
        attached = self.context.attached(state)
        if not any(item.num == inferior.num for item in attached.values()):
            connection = self.context.connection()
            endpoint = str(connection.details)
            match = re.fullmatch(r"(?:tcp:)?(.+):(\d+)", endpoint)
            if match is None or not 0 < int(match[2]) <= 65535:
                raise gdb.GdbError(f"Cannot infer pyOCD TCP ports from {endpoint!r}")
            state.reset()
            state.host, state.port = match[1], int(match[2])
            state.connection_type = connection.type
            state.attachments[0] = (inferior.num, connection.num)
        return state

    def list_cores(self) -> tuple[CoreInfo, ...]:
        """Read the server inventory without attaching other endpoints."""
        state = self._state()
        output = str(gdb.execute("monitor show cores", to_string=True))
        rows = re.findall(r"^\s*\*?\s*(\d+)\s+(\S+)\s+(.+?)\s*$", output, re.M)
        if not rows:
            raise gdb.GdbError("pyOCD did not return a CPU inventory (monitor show cores)")
        attached = self.context.attached(state)
        selected = gdb.selected_inferior().num
        return tuple(
            CoreInfo(
                int(number),
                f"{name} ({kind})",
                int(number) in attached and attached[int(number)].num == selected,
                f"{state.host}:{state.port + int(number)}",
                attached[int(number)].num if int(number) in attached else None,
            )
            for number, name, kind in rows
        )

    def select_core(self, core: CoreInfo) -> None:
        """Attach the requested pyOCD endpoint once and reuse its inferior."""
        state = self.context.session.state(CoreSessionState)
        if state.port + core.id > 65535:
            raise gdb.GdbError("pyOCD core port exceeds 65535")
        self.context.select_endpoint(core, state)
