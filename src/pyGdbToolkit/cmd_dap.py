"""Architecture-neutral GDB debug-access-port commands."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import gdb
from rich.console import Console
from rich.table import Table

from .ap_runtime import DEFAULT_AP_REGISTRY, AutoDebugPortTransport
from .arch.dap import AccessPortProfile, AccessPortRegistry
from .core_runtime import GdbCoreController
from .debug_port import AccessPort, DebugPortError, DebugPortTransport
from .session import SESSION, CommandHelp, CommandUsage, SessionSlice, ToolkitSession

CONSOLE = Console(force_terminal=True)
DAP_HELP = CommandHelp(
    "dap",
    "Inspect debug Access Ports (pyOCD/OpenOCD, APv1/APv2)",
    (
        CommandUsage("dap list", "List ports discovered by the connected server"),
        CommandUsage("dap core [list|<id>]", "Show, list or select physical GDB CPU cores"),
        CommandUsage("dap select <index>", "Select the server AP, without changing the GDB core"),
        CommandUsage("dap profile [<index>]", "Read identity and capabilities of an AP"),
        CommandUsage(
            "dap report <output.json>", "Collect a fresh JSON report of every discovered AP"
        ),
        CommandUsage("dap help", "Show command reference"),
    ),
    (
        "Automatically detects pyOCD or OpenOCD over the current GDB connection.",
        "pyOCD: connect initially to core 0 (lowest TCP port); other cores use port + ID.",
        "OpenOCD: select named hardware-core threads on the existing SMP connection.",
        "Profiling is read-only. Unknown or inaccessible capabilities are not inferred.",
    ),
)


@dataclass
class DapSessionState(SessionSlice):
    """Track the last confirmed server selection for the current connection."""

    selected_index: int | None = None

    def reset(self) -> None:
        """Forget target-dependent state on disconnect."""
        self.selected_index = None


def gdb_dap_transport() -> DebugPortTransport:
    """Connect the backend to GDB's monitor executor."""
    return AutoDebugPortTransport(lambda command: gdb.execute(command, to_string=True))


class DapCmd(gdb.Command):
    """Dispatch portable AP operations and render their evidence."""

    HELP = DAP_HELP

    def __init__(
        self,
        transport: DebugPortTransport | None = None,
        registry: AccessPortRegistry = DEFAULT_AP_REGISTRY,
        session: ToolkitSession = SESSION,
    ) -> None:
        """Register the prefix with injectable transport, registry and session."""
        super().__init__("dap", gdb.COMMAND_USER, gdb.COMPLETE_NONE, True)
        self.transport = transport if transport is not None else gdb_dap_transport()
        self.registry = registry
        self.session = session
        self.cores = GdbCoreController(session)
        session.register_command(self.HELP)

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Show help when the prefix is invoked directly."""
        del from_tty
        if arg.strip():
            raise gdb.GdbError("Unknown dap subcommand. Use 'dap help'.")
        self.run("help", [])

    def _ports(self) -> tuple[AccessPort, ...]:
        ports = self.transport.list_access_ports()
        self.session.state(DapSessionState).selected_index = next(
            (port.index for port in ports if port.selected), None
        )
        return ports

    def _port(self, ports: tuple[AccessPort, ...], token: str | None) -> AccessPort:
        index = self.session.state(DapSessionState).selected_index
        if token is not None:
            try:
                index = int(token, 0) if token.lower().startswith("0x") else int(token, 10)
            except ValueError as error:
                raise DebugPortError("AP index must be a decimal or hexadecimal integer") from error
        for port in ports:
            if port.index == index:
                return port
        raise DebugPortError("AP not discovered or no AP selected; use 'dap list'")

    def _architecture(self) -> str:
        return str(gdb.selected_inferior().architecture().name())

    def _profile(self, port: AccessPort) -> AccessPortProfile:
        return self.registry.provider(self._architecture()).profile(self.transport, port)

    def collect_report(self) -> dict[str, Any]:
        """Collect fresh profiles without changing AP selection."""
        ports = self._ports()
        architecture = self._architecture()
        provider = self.registry.provider(architecture)
        return {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "architecture": architecture,
            "backend": getattr(self.transport, "backend_name", type(self.transport).__name__),
            "discovery": getattr(self.transport, "discovery", "debug-server inventory"),
            "selected_ap": self.session.state(DapSessionState).selected_index,
            "access_ports": [
                dict(
                    provider.profile(self.transport, port).to_dict(),
                    server_name=port.name,
                    selected=port.selected,
                    ap_version=port.ap_version,
                )
                for port in ports
            ],
        }

    def run(self, name: str, args: list[str]) -> None:
        """Validate arguments and translate transport/file errors into GDB errors."""
        usage = next(entry.syntax for entry in DAP_HELP.usage if entry.syntax.split()[1] == name)
        valid = (
            not args
            if name in ("list", "help")
            else len(args) <= 1 if name in ("profile", "core") else len(args) == 1
        )
        if not valid:
            raise gdb.GdbError(f"Usage: {usage}")
        try:
            if name == "help":
                for entry in DAP_HELP.usage:
                    CONSOLE.print(f"{entry.syntax}: {entry.description}", markup=False)
                for note in DAP_HELP.notes:
                    CONSOLE.print(note, markup=False)
            elif name == "core":
                if args == ["list"]:
                    table = Table(title="CPU Cores")
                    for column in ("Core", "Name", "Endpoint", "Inferior", "Thread", "Selected"):
                        table.add_column(column)
                    for core in self.cores.list():
                        table.add_row(
                            str(core.id),
                            core.name,
                            core.endpoint,
                            str(core.inferior) if core.inferior is not None else "-",
                            str(core.thread) if core.thread is not None else "-",
                            "*" if core.selected else "",
                        )
                    CONSOLE.print(table)
                else:
                    if args:
                        if not args[0].isdecimal():
                            raise gdb.GdbError("Core ID must be a non-negative integer")
                        core = self.cores.select(int(args[0], 10))
                        self.session.state(DapSessionState).reset()
                    else:
                        core = self.cores.current()
                    CONSOLE.print(
                        f"Active core: {core.id} ({core.name}) | {core.endpoint}", markup=False
                    )
            elif name == "report":
                report = self.collect_report()
                path = Path(args[0]).expanduser()
                path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
                CONSOLE.print(f"Report written to {path}", markup=False)
            else:
                ports = self._ports()
                if name == "list":
                    table = Table(title="Access Ports")
                    for column in ("AP", "Server Type", "Selected"):
                        table.add_column(column)
                    for port in ports:
                        label = f"0x{port.index:x}" if port.ap_version == 2 else str(port.index)
                        table.add_row(label, port.name, "*" if port.selected else "")
                    CONSOLE.print(table)
                elif name == "select":
                    port = self._port(ports, args[0])
                    self.transport.select_access_port(port.index)
                    self.session.state(DapSessionState).selected_index = port.index
                    label = f"0x{port.index:x}" if port.ap_version == 2 else str(port.index)
                    CONSOLE.print(f"Selected AP {label} (GDB core unchanged)", markup=False)
                elif name == "profile":
                    profile = self._profile(self._port(ports, args[0] if args else None))
                    CONSOLE.print_json(json.dumps(profile.to_dict()))
        except (DebugPortError, OSError) as error:
            raise gdb.GdbError(str(error)) from error


class DapSubcommand(gdb.Command):
    """Register a concrete GDB subcommand with shared portable dispatch."""

    def __init__(self, parent: DapCmd, name: str) -> None:
        """Register one supported subcommand."""
        self.parent = parent
        self.name = name
        super().__init__(f"dap {name}", gdb.COMMAND_USER)

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Parse GDB arguments and dispatch to the prefix command."""
        del from_tty
        self.parent.run(self.name, list(gdb.string_to_argv(arg)))
