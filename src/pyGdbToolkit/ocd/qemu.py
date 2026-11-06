# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""QEMU emulated CPU contexts and checked HMP monitor execution."""

import re

import gdb

from ..session import SESSION
from .base import CoreBackend, CoreInfo
from .detection import OcdIdentifier, get_ocd


class QemuCoreBackend(CoreBackend):
    """Expose GDB threads in the attached QEMU CPU cluster, not physical cores."""

    identifier = OcdIdentifier.QEMU

    def list_cores(self) -> tuple[CoreInfo, ...]:
        """List emulated CPU threads in the current inferior only."""
        connection = self.context.connection()
        inferior = gdb.selected_inferior()
        selected = gdb.selected_thread()
        threads = sorted(inferior.threads(), key=lambda thread: thread.num)
        if not threads:
            raise gdb.GdbError("QEMU did not expose any emulated CPU threads")
        return tuple(
            CoreInfo(
                id=thread.num - 1,
                name=thread.name or f"QEMU CPU {thread.num - 1}",
                selected=selected is not None and thread.num == selected.num,
                endpoint=str(connection.details),
                inferior=inferior.num,
                thread=thread.global_num,
                scope="emulated-cpu-cluster",
                identity_source="qemu-gdb-thread",
            )
            for thread in threads
        )

    def select_core(self, core: CoreInfo) -> None:
        """Switch to an existing GDB thread without creating an attachment."""
        for thread in gdb.selected_inferior().threads():
            if thread.global_num == core.thread:
                thread.switch()
                return
        raise gdb.GdbError(f"QEMU CPU context {core.id} is no longer available")


def execute_monitor(command: str) -> str:
    """Execute HMP and translate unsupported commands and argument errors.

    Parameters
    ----------
    command : str
        QEMU HMP command without the GDB ``monitor`` prefix.

    Returns
    -------
    str
        Monitor output when the command succeeds.

    Raises
    ------
    gdb.GdbError
        If the connection is not QEMU, or HMP rejects the command.
    """
    if get_ocd().identifier != OcdIdentifier.QEMU:
        raise gdb.GdbError("QEMU monitor execution requires a connected QEMU GDB server")
    if not command.strip():
        raise gdb.GdbError("QEMU monitor requires a command; use 'monitor help'")
    try:
        output = str(gdb.execute(f"monitor {command}", to_string=True))
    except gdb.error as error:
        raise gdb.GdbError(f"QEMU monitor command {command!r} failed: {error}") from error
    if re.search(r"^unknown command:", output, re.M | re.I):
        raise gdb.GdbError(
            f"QEMU mode does not support monitor command {command!r}. "
            "OpenOCD/pyOCD probe commands are unavailable; use 'monitor help' "
            f"for QEMU HMP commands. {output.strip()}"
        )
    if re.search(
        r"^(?:.*: (?:extraneous characters|invalid parameter|string expected|"
        r"integer expression expected)|(?:Error|Parameter)\b|"
        r'Try "help [^"]+" for more information)',
        output,
        re.M | re.I,
    ):
        raise gdb.GdbError(f"QEMU monitor command {command!r} failed: {output.strip()}")
    if command.strip() == "system_reset":
        gdb.execute("maintenance flush register-cache", to_string=True)
        SESSION.invalidate()
    return output


class QemuMonitorCommand(gdb.Command):
    """Private checked monitor entry point used by the server's MI adapter."""

    def __init__(self) -> None:
        """Register without replacing GDB's native monitor command."""
        super().__init__("pygdb-qemu-monitor", gdb.COMMAND_USER)

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Raise real GDB errors even when HMP returned a successful RSP packet."""
        del from_tty
        gdb.write(execute_monitor(arg))
