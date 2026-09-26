"""GDB commands to select an RTOS and load its project symbols."""

import json
from pathlib import Path
import tomllib
from types import ModuleType

import gdb
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .rtos import SUPPORTED_RTOS

CONSOLE = Console(force_terminal=True)


class RtosCmd(gdb.Command):
    """Manage the selected RTOS."""

    def __init__(self) -> None:
        self.selected_rtos: ModuleType | None = None
        self.project_path: Path | None = None
        self.task_list: object | None = None
        self.scheduler_trace: SchedulerTrace | None = None
        super().__init__("rtos", gdb.COMMAND_USER, gdb.COMPLETE_NONE, True)

    def invoke(self, arg: str, from_tty: bool) -> None:
        del arg, from_tty
        table = Table(title="RTOS Commands", box=box.SIMPLE_HEAVY, header_style="bold cyan")
        table.add_column("Command", style="bold yellow")
        table.add_column("Description")
        table.add_row("rtos select <name>", "Select a supported RTOS")
        table.add_row("rtos list", "List supported RTOS implementations")
        table.add_row("rtos load-project --from <path>", "Load project symbols and task metadata")
        table.add_row("rtos show", "Show the kernel and task memory layout and metadata")
        table.add_row("rtos showsched <num>", "Trace the next num scheduler elections")
        CONSOLE.print(table)

    def _invoke_select(self, args: list[str]) -> None:
        if len(args) != 1:
            raise gdb.GdbError("Usage: rtos select <name>")
        name = args[0]
        if name not in SUPPORTED_RTOS:
            raise gdb.GdbError(f"Unsupported RTOS: {name} (supported: {', '.join(SUPPORTED_RTOS)})")
        self.selected_rtos = SUPPORTED_RTOS[name]
        if self.scheduler_trace is not None:
            self.scheduler_trace.delete()
        self.project_path = None
        self.task_list = None
        CONSOLE.print(Text.assemble("Selected RTOS: ", (name, "bold green")))

    def _invoke_list(self, args: list[str]) -> None:
        if args:
            raise gdb.GdbError("Usage: rtos list")
        table = Table(title="Supported RTOS", box=box.SIMPLE_HEAVY, header_style="bold cyan")
        table.add_column("Name")
        table.add_column("Status")
        for name, implementation in SUPPORTED_RTOS.items():
            status = "Selected" if implementation is self.selected_rtos else ""
            table.add_row(name, status)
        CONSOLE.print(table)
        if self.selected_rtos is None:
            CONSOLE.print("No RTOS selected.")

    def _invoke_load_project(self, args: list[str]) -> None:
        if len(args) != 2 or args[0] != "--from":
            raise gdb.GdbError("Usage: rtos load-project --from <path>")
        if self.selected_rtos is None:
            raise gdb.GdbError("Select an RTOS first with rtos select <name>")
        if self.scheduler_trace is not None:
            self.scheduler_trace.delete()
        self.project_path = None
        self.task_list = None
        project_path = Path(args[1]).expanduser().resolve()
        try:
            elfs = self.selected_rtos.project_elfs(project_path)
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
            task_list = self.selected_rtos.load_task_list(project_path, elfs[0])
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, gdb.error) as error:
            raise gdb.GdbError(f"Cannot load RTOS task metadata: {error}") from error
        self.task_list = task_list
        self.project_path = project_path

    def _invoke_show(self, args: list[str]) -> None:
        if args:
            raise gdb.GdbError("Usage: rtos show")
        if self.selected_rtos is None:
            raise gdb.GdbError("Select an RTOS first with rtos select <name>")
        if self.project_path is None:
            raise gdb.GdbError("Load a project first with rtos load-project --from <path>")
        try:
            CONSOLE.print(self.selected_rtos.show_project(self.project_path, self.task_list))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise gdb.GdbError(f"Cannot show RTOS project: {error}") from error

    def _invoke_showsched(self, args: list[str]) -> None:
        if len(args) != 1 or not args[0].isdecimal() or int(args[0]) < 1:
            raise gdb.GdbError("Usage: rtos showsched <num> (num must be positive)")
        if self.selected_rtos is None or self.task_list is None:
            raise gdb.GdbError("Select an RTOS and load a project before rtos showsched")
        if self.scheduler_trace is not None:
            self.scheduler_trace.delete()
        self.scheduler_trace = SchedulerTrace(self, int(args[0]))
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
        if self.is_valid():
            super().delete()
        if self.parent.scheduler_trace is self:
            self.parent.scheduler_trace = None

    def stop(self) -> bool:
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
        trace = self.trace
        try:
            value = self.return_value
            handle = int(value if value is not None else gdb.parse_and_eval("$r0"))
            task = trace.parent.selected_rtos.elected_task(trace.parent.task_list, handle)
        except (gdb.error, ValueError) as error:
            gdb.post_event(trace.delete)
            CONSOLE.print(f"[red]Cannot read elected task: {error}[/red]")
            return True
        trace.elections.append(task)
        if len(trace.elections) < trace.limit:
            return False
        gdb.post_event(trace.delete)
        CONSOLE.print(trace.parent.selected_rtos.scheduling_chart(trace.elections))
        return True


class _RtosSubcommand(gdb.Command):
    """Base class for concrete ``rtos`` subcommands registered under the prefix command."""

    def __init__(self, parent: RtosCmd, name: str) -> None:
        self.parent = parent
        self.name = name
        super().__init__(f"rtos {name}", gdb.COMMAND_USER)

    def _argv(self, arg: str) -> list[str]:
        return list(gdb.string_to_argv(arg)) if arg.strip() else []


class RtosSelectCmd(_RtosSubcommand):
    """Select a supported RTOS for the current GDB session."""

    def __init__(self, parent: RtosCmd) -> None:
        super().__init__(parent, "select")

    def invoke(self, arg: str, from_tty: bool) -> None:
        del from_tty
        self.parent._invoke_select(self._argv(arg))


class RtosListCmd(_RtosSubcommand):
    """List supported RTOS implementations and identify the selected one."""

    def __init__(self, parent: RtosCmd) -> None:
        super().__init__(parent, "list")

    def invoke(self, arg: str, from_tty: bool) -> None:
        del from_tty
        self.parent._invoke_list(self._argv(arg))


class RtosLoadProjectCmd(_RtosSubcommand):
    """Load symbols from a project of the selected RTOS."""

    def __init__(self, parent: RtosCmd) -> None:
        super().__init__(parent, "load-project")

    def invoke(self, arg: str, from_tty: bool) -> None:
        del from_tty
        self.parent._invoke_load_project(self._argv(arg))


class RtosShowCmd(_RtosSubcommand):
    """Show the kernel and task memory layout of the loaded RTOS project."""

    def __init__(self, parent: RtosCmd) -> None:
        super().__init__(parent, "show")

    def invoke(self, arg: str, from_tty: bool) -> None:
        del from_tty
        self.parent._invoke_show(self._argv(arg))


class RtosShowschedCmd(_RtosSubcommand):
    """Trace and chart the next scheduler elections."""

    def __init__(self, parent: RtosCmd) -> None:
        super().__init__(parent, "showsched")

    def invoke(self, arg: str, from_tty: bool) -> None:
        del from_tty
        self.parent._invoke_showsched(self._argv(arg))
