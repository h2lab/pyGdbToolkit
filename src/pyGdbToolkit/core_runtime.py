# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Select actual GDB CPU contexts behind pyOCD and OpenOCD connections."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import re
from typing import Any

import gdb

from .ocd import OCD, OcdDetector, OcdIdentifier
from .session import SESSION, SessionSlice, ToolkitSession


@dataclass(frozen=True)
class CoreInfo:
    """A physical CPU and its currently attached GDB context."""

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
    """Remember the initial, lowest pyOCD port and lazily attached cores."""

    host: str = ""
    port: int = 0
    connection_type: str = ""
    attachments: dict[int, tuple[int, int]] = field(default_factory=dict)

    def reset(self) -> None:
        """Discard connection identities, without disconnecting GDB."""
        self.host = ""
        self.port = 0
        self.connection_type = ""
        self.attachments.clear()


@dataclass
class JLinkCoreState(SessionSlice):
    """Bind the server-reported attached Cortex-M to one GDB connection."""

    connection_key: tuple[int, int] | None = None
    name: str = ""

    def reset(self) -> None:
        """Discard the attached-core identity."""
        self.connection_key = None
        self.name = ""


class GdbCoreController:
    """Share core discovery and verified selection between CLI and RPC."""

    def __init__(self, session: ToolkitSession = SESSION, detector: OcdDetector = OCD) -> None:
        """Use the current GDB connection and the shared toolkit session."""
        self.session = session
        self.detector = detector

    def _connection(self) -> Any:
        connection = getattr(gdb.selected_inferior(), "connection", None)
        if connection is None or connection.type not in ("remote", "extended-remote"):
            raise gdb.GdbError("Connect GDB to a pyOCD or OpenOCD server first")
        return connection

    def _backend(self) -> OcdIdentifier:
        self._connection()
        backend = self.detector.get().identifier
        if backend == OcdIdentifier.UNKNOWN:
            raise gdb.GdbError("Core selection requires pyOCD or OpenOCD")
        if backend not in (OcdIdentifier.PYOCD, OcdIdentifier.OPENOCD, OcdIdentifier.JLINK):
            raise gdb.GdbError(f"Hardware core discovery is not supported for OCD {backend}")
        return backend

    def register_jlink_core(self, name: str) -> None:
        """Register a Cortex-M identity reported by the connected J-Link server."""
        if self._backend() != OcdIdentifier.JLINK:
            raise gdb.GdbError("Attached-core registration requires J-Link")
        if re.fullmatch(r"Cortex-M\d+(?:\+|P)?", name) is None:
            raise gdb.GdbError("J-Link attached-core support requires a Cortex-M identity")
        inferior = gdb.selected_inferior()
        architecture = str(inferior.architecture().name()).lower()
        if not architecture.startswith("arm") or architecture.startswith("armv8-a"):
            raise gdb.GdbError("J-Link attached-core support does not include Cortex-A or Cortex-R")
        state = self.session.state(JLinkCoreState)
        state.connection_key = (int(inferior.num), int(self._connection().num))
        state.name = name

    def _jlink_cores(self) -> tuple[CoreInfo, ...]:
        inferior = gdb.selected_inferior()
        connection = self._connection()
        state = self.session.state(JLinkCoreState)
        if state.connection_key != (int(inferior.num), int(connection.num)):
            state.reset()
            raise gdb.GdbError(
                "J-Link attached Cortex-M identity is unavailable for this connection"
            )
        return (
            CoreInfo(
                0,
                state.name,
                True,
                str(connection.details),
                inferior.num,
                scope="attached-core-only",
                identity_source="jlink-server-connection-log",
            ),
        )

    def _attached(self, state: CoreSessionState) -> dict[int, Any]:
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

    def _pyocd_state(self) -> CoreSessionState:
        state = self.session.state(CoreSessionState)
        inferior = gdb.selected_inferior()
        attached = self._attached(state)
        if not any(item.num == inferior.num for item in attached.values()):
            connection = self._connection()
            endpoint = str(connection.details)
            match = re.fullmatch(r"(?:tcp:)?(.+):(\d+)", endpoint)
            if match is None or not 0 < int(match[2]) <= 65535:
                raise gdb.GdbError(f"Cannot infer pyOCD TCP ports from {endpoint!r}")
            state.reset()
            state.host, state.port = match[1], int(match[2])
            state.connection_type = connection.type
            state.attachments[0] = (inferior.num, connection.num)
        return state

    def _pyocd_cores(self) -> tuple[CoreInfo, ...]:
        state = self._pyocd_state()
        output = str(gdb.execute("monitor show cores", to_string=True))
        rows = re.findall(r"^\s*\*?\s*(\d+)\s+(\S+)\s+(.+?)\s*$", output, re.M)
        if not rows:
            raise gdb.GdbError("pyOCD did not return a CPU inventory (monitor show cores)")
        attached = self._attached(state)
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

    def _openocd_cores(self) -> tuple[CoreInfo, ...]:
        inferior = gdb.selected_inferior()
        selected = gdb.selected_thread()
        cores = []
        for thread in inferior.threads():
            name = thread.name or ""
            match = re.search(r"(?:^|[.])(?:cm|cpu|core|rv)(\d+)$", name)
            if match is None:
                raise gdb.GdbError(
                    "OpenOCD must expose named hardware-core threads in SMP mode; "
                    f"cannot identify core for thread {thread.global_num} ({name!r})"
                )
            cores.append(
                CoreInfo(
                    int(match[1]),
                    name,
                    thread == selected,
                    str(self._connection().details),
                    inferior.num,
                    thread.global_num,
                )
            )
        if not cores or len({core.id for core in cores}) != len(cores):
            raise gdb.GdbError("OpenOCD did not expose unique hardware-core threads")
        return tuple(sorted(cores, key=lambda core: core.id))

    def list(self) -> tuple[CoreInfo, ...]:
        """Discover CPUs without attaching other sockets or changing selection."""
        backend = self._backend()
        if backend == OcdIdentifier.JLINK:
            return self._jlink_cores()
        if backend == OcdIdentifier.PYOCD:
            return self._pyocd_cores()
        return self._openocd_cores()

    def current(self) -> CoreInfo:
        """Report the physical CPU corresponding to the selected GDB context."""
        for core in self.list():
            if core.selected:
                return core
        raise gdb.GdbError("No physical CPU corresponds to the selected GDB context")

    def _select_pyocd(self, core: CoreInfo) -> None:
        state = self.session.state(CoreSessionState)
        attached = self._attached(state)
        if core.id in attached:
            gdb.execute(f"inferior {attached[core.id].num}", to_string=True)
            return
        if state.port + core.id > 65535:
            raise gdb.GdbError("pyOCD core port exceeds 65535")
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
            state.attachments[core.id] = (created.num, connection.num)
        except Exception:
            if getattr(created, "connection", None) is not None:
                gdb.execute("disconnect", to_string=True)
            gdb.execute(f"inferior {previous.num}", to_string=True)
            if previous_thread is not None and previous_thread.is_valid():
                previous_thread.switch()
            gdb.execute(f"remove-inferiors {created.num}", to_string=True)
            raise

    def select(self, core_id: int) -> CoreInfo:
        """Select a discovered CPU and verify the resulting GDB context."""
        if isinstance(core_id, bool) or not isinstance(core_id, int) or core_id < 0:
            raise gdb.GdbError("Core ID must be a non-negative integer")
        core = next((core for core in self.list() if core.id == core_id), None)
        if core is None:
            raise gdb.GdbError(f"Core {core_id} not discovered; use 'dap core list'")
        if self._backend() == OcdIdentifier.JLINK:
            return core
        try:
            if self._backend() == OcdIdentifier.PYOCD:
                self._select_pyocd(core)
            else:
                thread = next(
                    thread
                    for thread in gdb.selected_inferior().threads()
                    if thread.global_num == core.thread
                )
                thread.switch()
            selected = self.current()
            if selected.id != core_id:
                raise gdb.GdbError(f"GDB did not select core {core_id}")
            return selected
        finally:
            self.session.invalidate()


CORES = GdbCoreController()
