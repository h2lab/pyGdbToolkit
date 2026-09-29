# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""The architecture-neutral ``fault_info`` GDB command entry point."""

from __future__ import annotations

import gdb
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .arch import (
    DEFAULT_DIAGNOSTIC_RUNTIME,
    DiagnosticPanel,
    DiagnosticReport,
    DiagnosticRuntime,
    DiagnosticServiceName,
    DiagnosticTable,
)
from .diagnostic_runtime import gdb_diagnostic_access
from .target_memory import TargetMemoryReader, TargetReadError

CONSOLE = Console(force_terminal=True)


def run_fault_analysis(
    runtime: DiagnosticRuntime = DEFAULT_DIAGNOSTIC_RUNTIME,
) -> DiagnosticReport:
    """Run the registered fault-analysis service against the selected target.

    Parameters
    ----------
    runtime
        Portable diagnostic runtime used to identify and collect the target report.

    Returns
    -------
    DiagnosticReport
        Renderer-independent diagnostic report returned by the selected service.

    Raises
    ------
    gdb.GdbError
        If target access fails or no service supports the selected target.
    """
    try:
        reader = TargetMemoryReader()
    except TargetReadError as error:
        raise gdb.GdbError(str(error)) from error
    result = runtime.diagnose(
        reader,
        DiagnosticServiceName.FAULT_ANALYSIS,
        gdb_diagnostic_access(),
    )
    if result.report is not None:
        return result.report
    if result.access_error:
        raise gdb.GdbError(result.unavailable_reason or "target unavailable")
    raise gdb.GdbError("fault_info only supports ARM Cortex-M targets")


def render_report(report: DiagnosticReport) -> None:
    """Render a generic diagnostic report through the shared Rich console."""
    blocks = report.blocks or (*report.tables, *report.panels)
    for block in blocks:
        if isinstance(block, DiagnosticTable):
            table = Table(
                title=block.title,
                box=box.SIMPLE_HEAVY,
                header_style="bold cyan",
                show_header=True,
            )
            for column in block.columns:
                table.add_column(column, style="bold" if column in {"Property", "Register"} else "")
            for row in block.rows:
                table.add_row(*("" if value is None else str(value) for value in row.values))
            CONSOLE.print(table)
        elif isinstance(block, DiagnosticPanel):
            CONSOLE.print(
                Panel(
                    "\n".join(block.lines),
                    title=f"[bold yellow]{block.title}[/bold yellow]",
                    box=box.SIMPLE_HEAVY,
                )
            )


class FaultInfoCmd(gdb.Command):
    """Run portable fault analysis and render the returned diagnostic report."""

    def __init__(self) -> None:
        """Register the command with GDB."""
        super().__init__("fault_info", gdb.COMMAND_USER)

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Execute ``fault_info`` without command-line arguments."""
        del from_tty
        if arg.strip():
            raise gdb.GdbError("fault_info does not accept arguments")
        render_report(run_fault_analysis())
