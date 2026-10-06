# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Backend-neutral GDB inferior attachment, reuse, and rollback."""

from __future__ import annotations

import json
from typing import Any, Callable

import gdb

from ..session import ToolkitSession
from .base import CoreInfo, CoreSessionState


class GdbCoreContext:
    """Share actual GDB context lifecycle operations across debug servers."""

    def __init__(self, session: ToolkitSession) -> None:
        """Use the session owning cached target inspection and backend state."""
        self.session = session

    def connection(self) -> Any:
        """Return the current remote connection, without identifying its backend."""
        connection = getattr(gdb.selected_inferior(), "connection", None)
        if connection is None or connection.type not in ("remote", "extended-remote"):
            raise gdb.GdbError("Connect GDB to a supported debug server first")
        return connection

    def attached(self, state: CoreSessionState) -> dict[int, Any]:
        """Resolve only attachments whose inferior and connection identities still match."""
        inferiors = {inferior.num: inferior for inferior in gdb.inferiors()}
        attached = {}
        for core, (number, connection_number) in state.attachments.items():
            inferior = inferiors.get(number)
            if inferior is None:
                continue
            connection = inferior.connection
            if connection is not None and connection.num == connection_number:
                attached[core] = inferior
        return attached

    def select_endpoint(
        self, core: CoreInfo, state: CoreSessionState, verify: Callable[[int], None] | None = None
    ) -> None:
        """Attach or reuse a GDB endpoint with optional backend identity verification."""
        attached = self.attached(state)
        if core.id in attached:
            gdb.execute(f"inferior {attached[core.id].num}", to_string=True)
            if verify is not None:
                verify(core.id)
            return
        previous = gdb.selected_inferior()
        previous_thread = gdb.selected_thread()
        architecture = previous.architecture().name()
        filename = previous.progspace.filename
        existing = {inferior.num for inferior in gdb.inferiors()}
        gdb.execute("add-inferior -no-connection", to_string=True)
        created = next(inferior for inferior in gdb.inferiors() if inferior.num not in existing)
        try:
            gdb.execute(f"inferior {created.num}", to_string=True)
            gdb.execute(f"set architecture {architecture}", to_string=True)
            if filename:
                gdb.execute(f"file {json.dumps(filename)}", to_string=True)
            gdb.execute(f"target {state.connection_type} {core.endpoint}", to_string=True)
            connection = created.connection
            if connection is None:
                raise gdb.GdbError("GDB did not attach the requested core")
            if verify is not None:
                verify(core.id)
            state.attachments[core.id] = (created.num, connection.num)
        except Exception:
            if getattr(created, "connection", None) is not None:
                gdb.execute("disconnect", to_string=True)
            gdb.execute(f"inferior {previous.num}", to_string=True)
            if previous_thread is not None and previous_thread.is_valid():
                previous_thread.switch()
            gdb.execute(f"remove-inferiors {created.num}", to_string=True)
            raise
