# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""J-Link attached-core and configured-endpoint CPU selection strategies."""

from __future__ import annotations

from dataclasses import dataclass, field
import re

import gdb

from ..diagnostic_runtime import GdbDiagnosticRegisterReader
from ..session import SessionSlice
from .base import CoreBackend, CoreInfo, CoreSessionState
from .detection import OcdIdentifier


def selected_core_identity() -> tuple[str, str]:
    """Return architectural affinity when readable, otherwise endpoint evidence."""
    inferior = gdb.selected_inferior()
    architecture = str(inferior.architecture().name()).lower()
    if architecture.startswith("aarch64"):
        value = GdbDiagnosticRegisterReader().read_first(("mpidr_el1", "MPIDR_EL1"))
        if value is not None:
            return f"mpidr:{value & 0xFF00FFFFFF:010x}", "mpidr"
        output = str(gdb.execute("monitor cp15 0,0,0,5", to_string=True))
        match = re.fullmatch(
            r"\s*Reading CP15 register \(0,0,0,5 = (0x[0-9A-Fa-f]{1,8})\)\s*", output
        )
        if match is None or not int(match[1], 16) & 0x80000000:
            raise gdb.GdbError("J-Link did not confirm the selected CPU affinity via MPIDR")
        return f"mpidr-low24:{int(match[1], 16) & 0xFFFFFF:06x}", "mpidr-low24"
    connection = getattr(inferior, "connection", None)
    if connection is None:
        raise gdb.GdbError("J-Link CPU endpoint is not connected")
    return f"endpoint:{connection.details}", "configured-endpoint"


@dataclass
class JLinkClusterState(CoreSessionState):
    """Configured J-Link endpoints and observed per-core identity evidence."""

    endpoints: dict[int, str] = field(default_factory=dict)
    identities: dict[int, tuple[str, str]] = field(default_factory=dict)
    names: dict[int, str] = field(default_factory=dict)

    def reset(self) -> None:
        """Discard the configured cluster and its attachments."""
        super().reset()
        self.endpoints.clear()
        self.identities.clear()
        self.names.clear()


@dataclass
class JLinkCoreState(SessionSlice):
    """Bind the server-reported attached Cortex-M to one GDB connection."""

    connection_key: tuple[int, int] | None = None
    name: str = ""

    def reset(self) -> None:
        """Discard the attached-core identity."""
        self.connection_key = None
        self.name = ""


class JLinkCoreBackend(CoreBackend):
    """Select configured CPU endpoints or expose the single reported attached CPU."""

    identifier = OcdIdentifier.JLINK

    @property
    def invalidates_selection(self) -> bool:
        """Preserve the legacy attached-core no-op without invalidating inspection."""
        return bool(self.context.session.state(JLinkClusterState).endpoints)

    def register_cluster(
        self, endpoints: dict[int, str], initial_core: int, devices: dict[int, str] | None = None
    ) -> None:
        """Register CPU endpoints without deriving core IDs from architecture topology."""
        if (
            isinstance(initial_core, bool)
            or len(set(endpoints.values())) != len(endpoints)
            or initial_core not in endpoints
            or any(
                isinstance(core, bool)
                or not isinstance(core, int)
                or core < 0
                or re.fullmatch(r"127\.0\.0\.1:\d+", endpoint) is None
                or not 0 < int(endpoint.rsplit(":", 1)[1]) <= 65535
                for core, endpoint in endpoints.items()
            )
        ):
            raise gdb.GdbError("Invalid J-Link cluster endpoints")
        if devices is not None and (
            set(devices) != set(endpoints) or any(not name.strip() for name in devices.values())
        ):
            raise gdb.GdbError("Cluster device names must match the configured endpoint IDs")
        identity = selected_core_identity()
        state = self.context.session.state(JLinkClusterState)
        state.reset()
        state.endpoints = dict(endpoints)
        state.names = dict(devices or {core: f"Core {core}" for core in endpoints})
        state.identities[initial_core] = identity
        inferior = gdb.selected_inferior()
        state.attachments[initial_core] = (inferior.num, self.context.connection().num)

    def register_attached_core(self, name: str) -> None:
        """Register a Cortex-M identity reported by the connected J-Link server."""
        if re.fullmatch(r"Cortex-M\d+(?:\+|P)?", name) is None:
            raise gdb.GdbError("J-Link attached-core support requires a Cortex-M identity")
        inferior = gdb.selected_inferior()
        architecture = str(inferior.architecture().name()).lower()
        if not architecture.startswith("arm") or architecture.startswith("armv8-a"):
            raise gdb.GdbError("J-Link attached-core support does not include Cortex-A or Cortex-R")
        state = self.context.session.state(JLinkCoreState)
        state.connection_key = (int(inferior.num), int(self.context.connection().num))
        state.name = name

    def _verify_identity(self, core_id: int) -> None:
        state = self.context.session.state(JLinkClusterState)
        identity = selected_core_identity()
        if (core_id in state.identities and state.identities[core_id] != identity) or any(
            other != core_id and evidence[0] == identity[0]
            for other, evidence in state.identities.items()
        ):
            raise gdb.GdbError(
                f"J-Link did not confirm a distinct and stable identity for core {core_id}"
            )
        state.identities[core_id] = identity

    def _cluster_cores(self, state: JLinkClusterState) -> tuple[CoreInfo, ...]:
        attached = self.context.attached(state)
        selected = gdb.selected_inferior().num
        if not any(inferior.num == selected for inferior in attached.values()):
            state.reset()
            raise gdb.GdbError("J-Link cluster identity is unavailable for this connection")
        return tuple(
            CoreInfo(
                core,
                state.names[core],
                core in attached and attached[core].num == selected,
                endpoint,
                attached[core].num if core in attached else None,
                scope="configured-core-cluster",
                identity_source=(
                    "configuration+" + state.identities[core][1]
                    if core in state.identities
                    else "configuration"
                ),
            )
            for core, endpoint in sorted(state.endpoints.items())
        )

    def list_cores(self) -> tuple[CoreInfo, ...]:
        """List configured endpoints or the connection-bound attached-core identity."""
        cluster = self.context.session.state(JLinkClusterState)
        if cluster.endpoints:
            return self._cluster_cores(cluster)
        inferior = gdb.selected_inferior()
        connection = self.context.connection()
        state = self.context.session.state(JLinkCoreState)
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

    def select_core(self, core: CoreInfo) -> None:
        """Select and verify a cluster endpoint or preserve the attached-core no-op."""
        state = self.context.session.state(JLinkClusterState)
        if not state.endpoints:
            return
        previous = gdb.selected_inferior()
        try:
            state.connection_type = "remote"
            self.context.select_endpoint(core, state, self._verify_identity)
        except Exception:
            gdb.execute(f"inferior {previous.num}", to_string=True)
            raise
