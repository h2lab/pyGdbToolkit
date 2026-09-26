"""GDB command to select the active RTOS."""

from types import ModuleType

import gdb

from .rtos import SUPPORTED_RTOS


class RtosCmd(gdb.Command):
    """Manage the selected RTOS."""

    def __init__(self) -> None:
        self.selected_rtos: ModuleType | None = None
        super().__init__("rtos", gdb.COMMAND_USER, gdb.COMPLETE_NONE, True)

    def invoke(self, arg: str, from_tty: bool) -> None:
        del arg, from_tty
        gdb.write(f"Usage: rtos select <name> (supported: {', '.join(SUPPORTED_RTOS)})\n")

    def _invoke_select(self, args: list[str]) -> None:
        if len(args) != 1:
            raise gdb.GdbError("Usage: rtos select <name>")
        name = args[0]
        if name not in SUPPORTED_RTOS:
            raise gdb.GdbError(
                f"Unsupported RTOS: {name} (supported: {', '.join(SUPPORTED_RTOS)})"
            )
        self.selected_rtos = SUPPORTED_RTOS[name]
        gdb.write(f"Selected RTOS: {name}\n")


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
