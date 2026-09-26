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
        CONSOLE.print(table)

    def _invoke_select(self, args: list[str]) -> None:
        if len(args) != 1:
            raise gdb.GdbError("Usage: rtos select <name>")
        name = args[0]
        if name not in SUPPORTED_RTOS:
            raise gdb.GdbError(f"Unsupported RTOS: {name} (supported: {', '.join(SUPPORTED_RTOS)})")
        self.selected_rtos = SUPPORTED_RTOS[name]
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
