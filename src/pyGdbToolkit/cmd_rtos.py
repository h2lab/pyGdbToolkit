# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""GDB commands to select an RTOS and load its project symbols."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import tomllib
from types import ModuleType
import xml.etree.ElementTree as ET

import gdb
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .rtos import SUPPORTED_RTOS
from .session import SESSION as TOOLKIT_SESSION
from .session import SessionSlice

CONSOLE = Console(force_terminal=True)


@dataclass
class RtosSessionState(SessionSlice):
    """Encapsulate the RTOS selection, its loaded project, and the active trace."""

    selected_rtos: ModuleType | None = None
    project_path: Path | None = None
    task_list: object | None = None
    scheduler_trace: SchedulerTrace | None = None

    def clear_project(self) -> None:
        """Delete the active scheduler trace and drop the loaded project metadata."""
        if self.scheduler_trace is not None:
            self.scheduler_trace.delete()
        self.project_path = None
        self.task_list = None

    def reset(self) -> None:
        """Drop the loaded project metadata and the RTOS selection."""
        self.clear_project()
        self.selected_rtos = None


class RtosCmd(gdb.Command):
    """Manage the selected RTOS."""

    def __init__(self) -> None:
        """Register the prefix command and bind the session-owned RTOS state."""
        self.state = TOOLKIT_SESSION.state(RtosSessionState)
        super().__init__("rtos", gdb.COMMAND_USER, gdb.COMPLETE_NONE, True)

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Show the RTOS command reference."""
        del arg, from_tty
        table = Table(title="RTOS Commands", box=box.SIMPLE_HEAVY, header_style="bold cyan")
        table.add_column("Command", style="bold yellow")
        table.add_column("Description")
        table.add_row("rtos select <name>", "Select a supported RTOS")
        table.add_row("rtos list", "List supported RTOS implementations")
        table.add_row("rtos load-project --from <path>", "Load project symbols and task metadata")
        table.add_row("rtos show", "Show the kernel and task memory layout and metadata")
        table.add_row("rtos show task <taskname>", "Inspect a task on the stopped target")
        table.add_row("rtos showsched <num>", "Trace the next num scheduler elections")
        CONSOLE.print(table)

    def _invoke_select(self, args: list[str]) -> None:
        if len(args) != 1:
            raise gdb.GdbError("Usage: rtos select <name>")
        name = args[0]
        if name not in SUPPORTED_RTOS:
            raise gdb.GdbError(f"Unsupported RTOS: {name} (supported: {', '.join(SUPPORTED_RTOS)})")
        self.state.selected_rtos = SUPPORTED_RTOS[name]
        self.state.clear_project()
        CONSOLE.print(Text.assemble("Selected RTOS: ", (name, "bold green")))

    def _invoke_list(self, args: list[str]) -> None:
        if args:
            raise gdb.GdbError("Usage: rtos list")
        table = Table(title="Supported RTOS", box=box.SIMPLE_HEAVY, header_style="bold cyan")
        table.add_column("Name")
        table.add_column("Status")
        for name, implementation in SUPPORTED_RTOS.items():
            status = "Selected" if implementation is self.state.selected_rtos else ""
            table.add_row(name, status)
        CONSOLE.print(table)
        if self.state.selected_rtos is None:
            CONSOLE.print("No RTOS selected.")

    def _invoke_load_project(self, args: list[str]) -> None:
        if len(args) != 2 or args[0] != "--from":
            raise gdb.GdbError("Usage: rtos load-project --from <path>")
        if self.state.selected_rtos is None:
            raise gdb.GdbError("Select an RTOS first with rtos select <name>")
        self.state.clear_project()
        project_path = Path(args[1]).expanduser().resolve()
        try:
            elfs = self.state.selected_rtos.project_elfs(project_path)
        except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
            raise gdb.GdbError(f"Cannot load RTOS project: {error}") from error
        if not elfs or elfs[0].name != "sentry-kernel.elf":
            raise gdb.GdbError("Cannot load RTOS task metadata: kernel ELF is required")
        for elf in elfs:
            escaped = str(elf).replace("\\", "\\\\").replace('"', '\\"')
            gdb.execute(f'add-symbol-file "{escaped}" -o 0', from_tty=False, to_string=True)
            CONSOLE.print(
                Text.assemble("Loaded symbols: ", (str(elf.relative_to(project_path)), "green"))
            )
        try:
            task_list = self.state.selected_rtos.load_task_list(project_path, elfs[0])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, gdb.error) as error:
            raise gdb.GdbError(f"Cannot load RTOS task metadata: {error}") from error
        self.state.task_list = task_list
        self.state.project_path = project_path

    def _invoke_show(self, args: list[str]) -> None:
        if args:
            raise gdb.GdbError("Usage: rtos show")
        if self.state.selected_rtos is None:
            raise gdb.GdbError("Select an RTOS first with rtos select <name>")
        if self.state.project_path is None:
            raise gdb.GdbError("Load a project first with rtos load-project --from <path>")
        try:
            CONSOLE.print(
                self.state.selected_rtos.show_project(self.state.project_path, self.state.task_list)
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise gdb.GdbError(f"Cannot show RTOS project: {error}") from error

    def _invoke_show_task(self, args: list[str]) -> None:
        if len(args) != 1:
            raise gdb.GdbError("Usage: rtos show task <taskname>")
        state = self.state
        if state.selected_rtos is None or state.project_path is None or state.task_list is None:
            raise gdb.GdbError("Select an RTOS and load a project before rtos show task")
        try:
            CONSOLE.print(
                state.selected_rtos.show_task(state.project_path, state.task_list, args[0])
            )
        except (OSError, ValueError, KeyError, TypeError, ET.ParseError, gdb.error) as error:
            raise gdb.GdbError(f"Cannot show task {args[0]}: {error}") from error

    def _invoke_showsched(self, args: list[str]) -> None:
        if len(args) != 1 or not args[0].isdecimal() or int(args[0]) < 1:
            raise gdb.GdbError("Usage: rtos showsched <num> (num must be positive)")
        if self.state.selected_rtos is None or self.state.task_list is None:
            raise gdb.GdbError("Select an RTOS and load a project before rtos showsched")
        if self.state.scheduler_trace is not None:
            self.state.scheduler_trace.delete()
        self.state.scheduler_trace = SchedulerTrace(self, int(args[0]))
        CONSOLE.print(
            f"Tracing {args[0]} scheduler elections. Continue the target to collect them."
        )


class SchedulerTrace(gdb.Breakpoint):
    """Trace elections at sched_elect entry, sampling the elected handle on return."""

    def __init__(self, parent: RtosCmd, limit: int) -> None:
        self.parent = parent
        self.limit = limit
        self.elections: list[str] = []
        super().__init__("sched_elect", internal=True)

    def delete(self) -> None:
        """Remove the breakpoint and clear the active scheduler trace."""
        if self.is_valid():
            super().delete()
        if self.parent.state.scheduler_trace is self:
            self.parent.state.scheduler_trace = None

    def stop(self) -> bool:
        """Place a finish breakpoint to observe the elected task."""
        try:
            _ElectionReturn(self)
        except gdb.error as error:
            gdb.post_event(self.delete)
            CONSOLE.print(f"[red]Cannot trace scheduler return: {error}[/red]")
            return True
        return False


class _ElectionReturn(gdb.FinishBreakpoint):
    """Capture the elected task after sched_elect updates scheduler state."""

    def __init__(self, trace: SchedulerTrace) -> None:
        self.trace = trace
        super().__init__(gdb.newest_frame(), internal=True)

    def stop(self) -> bool:
        """Record the returned task and stop when the requested count is reached."""
        trace = self.trace
        try:
            selected_rtos = trace.parent.state.selected_rtos
            if selected_rtos is None:
                raise ValueError("No RTOS selected during scheduler trace")
            value = self.return_value
            handle = int(value if value is not None else gdb.parse_and_eval("$r0"))
            task = selected_rtos.elected_task(trace.parent.state.task_list, handle)
        except (gdb.error, ValueError) as error:
            gdb.post_event(trace.delete)
            CONSOLE.print(f"[red]Cannot read elected task: {error}[/red]")
            return True
        trace.elections.append(task)
        if len(trace.elections) < trace.limit:
            return False
        gdb.post_event(trace.delete)
        CONSOLE.print(selected_rtos.scheduling_chart(trace.elections))
        return True


class _RtosSubcommand(gdb.Command):
    """Base class for concrete ``rtos`` subcommands registered under the prefix command."""

    def __init__(self, parent: RtosCmd, name: str, is_prefix: bool = False) -> None:
        self.parent = parent
        self.name = name
        super().__init__(f"rtos {name}", gdb.COMMAND_USER, gdb.COMPLETE_NONE, is_prefix)

    def _argv(self, arg: str) -> list[str]:
        return list(gdb.string_to_argv(arg)) if arg.strip() else []


class RtosSelectCmd(_RtosSubcommand):
    """Select a supported RTOS for the current GDB session."""

    def __init__(self, parent: RtosCmd) -> None:
        super().__init__(parent, "select")

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Select a supported RTOS."""
        del from_tty
        self.parent._invoke_select(self._argv(arg))


class RtosListCmd(_RtosSubcommand):
    """List supported RTOS implementations and identify the selected one."""

    def __init__(self, parent: RtosCmd) -> None:
        super().__init__(parent, "list")

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Display supported RTOS implementations and the current selection."""
        del from_tty
        self.parent._invoke_list(self._argv(arg))


class RtosLoadProjectCmd(_RtosSubcommand):
    """Load symbols from a project of the selected RTOS."""

    def __init__(self, parent: RtosCmd) -> None:
        super().__init__(parent, "load-project")

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Load project symbols and task metadata for the selected RTOS."""
        del from_tty
        self.parent._invoke_load_project(self._argv(arg))


class RtosShowCmd(_RtosSubcommand):
    """Show the kernel and task memory layout of the loaded RTOS project."""

    def __init__(self, parent: RtosCmd) -> None:
        super().__init__(parent, "show", True)

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Display the loaded project's task memory layout."""
        del from_tty
        self.parent._invoke_show(self._argv(arg))


class RtosShowTaskCmd(_RtosSubcommand):
    """Inspect a task's live kernel context on the stopped target."""

    def __init__(self, parent: RtosCmd) -> None:
        super().__init__(parent, "show task")

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Display a task's live context and saved stack."""
        del from_tty
        self.parent._invoke_show_task(self._argv(arg))


class RtosShowschedCmd(_RtosSubcommand):
    """Trace and chart the next scheduler elections."""

    def __init__(self, parent: RtosCmd) -> None:
        super().__init__(parent, "showsched")

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Trace the requested number of scheduler elections."""
        del from_tty
        self.parent._invoke_showsched(self._argv(arg))
