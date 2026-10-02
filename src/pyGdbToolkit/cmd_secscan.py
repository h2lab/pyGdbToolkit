# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""The architecture-neutral ``secscan`` GDB command entry point."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import html
import json
from pathlib import Path
from typing import Any

import gdb
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .arch import (
    DiagnosticReport,
    DiagnosticServiceName,
    DiagnosticSeverity,
)
from .diagnostic_runtime import gdb_diagnostic_access
from .session import SESSION, CommandHelp, CommandUsage, ToolkitSession
from .target_memory import TargetReadError

CONSOLE = Console(force_terminal=True)


@dataclass(frozen=True)
class SecscanFinding:
    """One security-audit observation."""

    category: str
    severity: str
    title: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        """Convert this finding to a plain dictionary."""
        return {
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SecscanFinding:
        """Rebuild a finding from its dictionary representation."""
        return cls(
            category=str(data["category"]),
            severity=str(data["severity"]),
            title=str(data["title"]),
            detail=str(data["detail"]),
        )


@dataclass(frozen=True)
class SecscanReport:
    """A complete security-posture audit report rendered by this command."""

    core: str
    vendor: str | None
    device_name: str | None
    generated_at: str
    findings: tuple[SecscanFinding, ...]

    def counts(self) -> dict[str, int]:
        """Return the number of findings per established output severity."""
        counts = {"FAIL": 0, "WARN": 0, "INFO": 0, "PASS": 0}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        """Convert the complete report to the stable JSON schema."""
        return {
            "core": self.core,
            "vendor": self.vendor,
            "device_name": self.device_name,
            "generated_at": self.generated_at,
            "summary": self.counts(),
            "findings": [finding.to_dict() for finding in self.findings],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SecscanReport:
        """Rebuild a report from its stable JSON schema."""
        return cls(
            core=str(data["core"]),
            vendor=data.get("vendor"),
            device_name=data.get("device_name"),
            generated_at=str(data["generated_at"]),
            findings=tuple(SecscanFinding.from_dict(finding) for finding in data["findings"]),
        )


_SEVERITY_NAMES = {
    DiagnosticSeverity.PASS: "PASS",
    DiagnosticSeverity.INFO: "INFO",
    DiagnosticSeverity.WARNING: "WARN",
    DiagnosticSeverity.ERROR: "FAIL",
    DiagnosticSeverity.CRITICAL: "FAIL",
}


def run_audit(session: ToolkitSession = SESSION) -> SecscanReport:
    """Run the registered security-audit service against the selected target.

    Parameters
    ----------
    session : ToolkitSession
        Unified session providing target access and the portable diagnostic
        runtime.  Dependency injection keeps command tests independent of a
        live GDB target.

    Returns
    -------
    SecscanReport
        The output-compatible report model.

    Raises
    ------
    gdb.GdbError
        If no registered architecture can perform the requested audit.
    """
    try:
        result = session.diagnose(
            DiagnosticServiceName.SECURITY_AUDIT,
            gdb_diagnostic_access(),
        )
    except TargetReadError as error:
        raise gdb.GdbError(str(error)) from error
    if result.report is None:
        if result.access_error:
            raise gdb.GdbError(result.unavailable_reason or "target unavailable")
        raise gdb.GdbError("secscan only supports ARM Cortex-M targets")
    return _render_model(result.report)


def _render_model(report: DiagnosticReport) -> SecscanReport:
    """Adapt a portable diagnostic report to the stable secscan file schema."""
    return SecscanReport(
        core=report.target.core_name,
        vendor=_target_field(report, "vendor"),
        device_name=_target_field(report, "device_name"),
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        findings=tuple(
            SecscanFinding(
                finding.category,
                _SEVERITY_NAMES.get(finding.severity, finding.severity.value.upper()),
                finding.title,
                finding.detail,
            )
            for finding in report.findings
        ),
    )


def _target_field(report: DiagnosticReport, name: str) -> str | None:
    """Return a string target metadata field supplied by a diagnostic service."""
    for section in report.sections:
        for field in section.fields:
            if field.name == name and isinstance(field.value, str):
                return field.value
    return None


_SEVERITY_STYLE = {
    "FAIL": "bold red",
    "WARN": "bold yellow",
    "INFO": "cyan",
    "PASS": "bold green",
}


def render_report(report: SecscanReport) -> None:
    """Render a security-audit report through the shared Rich console."""
    counts = report.counts()
    summary = (
        f"Core: {report.core}  |  Vendor: {report.vendor or 'Unknown'}  |  "
        f"Device: {report.device_name or 'Unknown'}\n"
        f"Generated: {report.generated_at}\n"
        f"[bold red]FAIL: {counts['FAIL']}[/bold red]  "
        f"[bold yellow]WARN: {counts['WARN']}[/bold yellow]  "
        f"[cyan]INFO: {counts['INFO']}[/cyan]  "
        f"[bold green]PASS: {counts['PASS']}[/bold green]"
    )
    CONSOLE.print(Panel(summary, title="secscan audit summary", box=box.SIMPLE_HEAVY))

    categories: list[str] = []
    for finding in report.findings:
        if finding.category not in categories:
            categories.append(finding.category)
    for category in categories:
        table = Table(
            title=category,
            box=box.SIMPLE_HEAVY,
            header_style="bold cyan",
            show_header=True,
        )
        table.add_column("Severity", no_wrap=True)
        table.add_column("Finding", style="bold")
        table.add_column("Detail")
        for finding in report.findings:
            if finding.category == category:
                table.add_row(
                    Text(finding.severity, style=_SEVERITY_STYLE.get(finding.severity, "")),
                    finding.title,
                    finding.detail,
                )
        CONSOLE.print(table)


def dump_report_to_json(report: SecscanReport, output_path: Path) -> int:
    """Serialize an audit report to a JSON file and return its byte size."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as json_file:
        json.dump(report.to_dict(), json_file, indent=2)
    return output_path.stat().st_size


_HTML_STYLE = """
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; color: #1a1a1a; }
  h1 { margin-bottom: 0.25rem; }
  .meta { color: #555; margin-bottom: 1.5rem; }
  .summary { display: flex; gap: 1rem; margin-bottom: 2rem; }
  .badge { padding: 0.5rem 1rem; border-radius: 6px; font-weight: 600; color: #fff; }
  .badge.fail { background: #c0392b; }
  .badge.warn { background: #b9770e; }
  .badge.info { background: #2874a6; }
  .badge.pass { background: #1e8449; }
  section { margin-bottom: 2rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border: 1px solid #ddd; padding: 0.5rem 0.75rem; text-align: left; vertical-align: top; }
  th { background: #f4f4f4; }
  td.sev { font-weight: 700; white-space: nowrap; }
  tr.sev-fail td.sev { color: #c0392b; }
  tr.sev-warn td.sev { color: #b9770e; }
  tr.sev-info td.sev { color: #2874a6; }
  tr.sev-pass td.sev { color: #1e8449; }
"""

_HTML_SEVERITY_CLASS = {
    "FAIL": "sev-fail",
    "WARN": "sev-warn",
    "INFO": "sev-info",
    "PASS": "sev-pass",
}


def _html_finding_row(finding: SecscanFinding) -> str:
    """Render one finding as an HTML table row, escaping all user-facing text."""
    row_class = _HTML_SEVERITY_CLASS.get(finding.severity, "")
    severity = html.escape(finding.severity)
    title = html.escape(finding.title)
    detail = html.escape(finding.detail)
    return (
        f'<tr class="{row_class}"><td class="sev">{severity}</td>'
        f"<td>{title}</td><td>{detail}</td></tr>"
    )


def generate_html_report(report: SecscanReport) -> str:
    """Render a security-audit report as a standalone HTML document."""
    counts = report.counts()
    categories: list[str] = []
    for finding in report.findings:
        if finding.category not in categories:
            categories.append(finding.category)
    sections: list[str] = []
    for category in categories:
        rows = "\n".join(
            _html_finding_row(finding)
            for finding in report.findings
            if finding.category == category
        )
        sections.append(
            f"<section><h2>{html.escape(category)}</h2>"
            "<table><thead><tr><th>Severity</th><th>Finding</th><th>Detail</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></section>"
        )
    head = (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f"<title>secscan report - {html.escape(report.core)}</title>\n"
        f"<style>{_HTML_STYLE}</style>\n</head>\n<body>\n"
    )
    meta = (
        "<h1>secscan audit report</h1>\n"
        f'<p class="meta">Core: {html.escape(report.core)} &middot; '
        f"Vendor: {html.escape(report.vendor or 'Unknown')} &middot; "
        f"Device: {html.escape(report.device_name or 'Unknown')} &middot; "
        f"Generated: {html.escape(report.generated_at)}</p>\n"
    )
    summary = (
        '<div class="summary">'
        f'<span class="badge fail">FAIL {counts["FAIL"]}</span>'
        f'<span class="badge warn">WARN {counts["WARN"]}</span>'
        f'<span class="badge info">INFO {counts["INFO"]}</span>'
        f'<span class="badge pass">PASS {counts["PASS"]}</span>'
        "</div>\n"
    )
    return head + meta + summary + "\n".join(sections) + "\n</body>\n</html>\n"


def dump_report_to_html(report: SecscanReport, output_path: Path) -> int:
    """Write a standalone HTML report and return its byte size."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as html_file:
        html_file.write(generate_html_report(report))
    return output_path.stat().st_size


def load_report_from_json(input_path: Path) -> SecscanReport:
    """Load a previously dumped audit report from a JSON file."""
    with input_path.open("r", encoding="utf-8") as json_file:
        data = json.load(json_file)
    try:
        return SecscanReport.from_dict(data)
    except (KeyError, TypeError) as error:
        raise ValueError(f"'{input_path}' is not a valid secscan report: {error}") from error


SECSCAN_HELP = CommandHelp(
    name="secscan",
    summary="Audit the target security configuration and render or export the report.",
    usage=(
        CommandUsage(
            "secscan audit [<output.json>]",
            "Run the full security audit against the live target and optionally dump it to JSON",
        ),
        CommandUsage(
            "secscan report <report.json> [--html <output.html>]",
            "Render a clean report from a previously dumped audit file (console by default, "
            "or a standalone HTML file with --html)",
        ),
        CommandUsage("secscan help", "Show this command reference"),
    ),
)


def render_help() -> None:
    """Render the secscan command overview and help table."""
    table = Table(
        title="Cortex-M Security Posture Audit",
        box=box.SIMPLE_HEAVY,
        header_style="bold cyan",
        show_header=True,
    )
    table.add_column("Command Syntax", style="bold yellow", no_wrap=True)
    table.add_column("Description")
    for entry in SECSCAN_HELP.usage:
        table.add_row(entry.syntax, entry.description)
    CONSOLE.print(table)


class SecscanCmd(gdb.Command):
    """Run a registered security audit and render its portable report."""

    HELP = SECSCAN_HELP

    def __init__(self) -> None:
        """Register the command with GDB."""
        super().__init__("secscan", gdb.COMMAND_USER, gdb.COMPLETE_NONE, True)
        SESSION.register_command(self.HELP)

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Display the help overview."""
        del arg
        del from_tty
        render_help()

    def _invoke_audit(self, args: list[str]) -> None:
        """Handle ``secscan audit [<output.json>]``."""
        if len(args) > 1:
            raise gdb.GdbError("Usage: secscan audit [<output.json>]")
        report = run_audit()
        render_report(report)
        if args:
            out_path = Path(args[0]).expanduser().resolve()
            try:
                dump_report_to_json(report, out_path)
            except OSError as error:
                raise gdb.GdbError(f"Failed to write dump to '{out_path}': {error}") from error
            CONSOLE.print(f"[green]Report written to {out_path}[/green]")

    def _invoke_report(self, args: list[str]) -> None:
        """Handle ``secscan report <report.json> [--html <output.html>]``."""
        usage = "Usage: secscan report <report.json> [--html <output.html>]"
        html_arg: str | None = None
        positional: list[str] = []
        index = 0
        while index < len(args):
            token = args[index]
            if token == "--html":
                if index + 1 >= len(args):
                    raise gdb.GdbError(f"--html requires an output file path. {usage}")
                html_arg = args[index + 1]
                index += 2
                continue
            positional.append(token)
            index += 1
        if len(positional) != 1:
            raise gdb.GdbError(usage)
        input_path = Path(positional[0]).expanduser().resolve()
        try:
            report = load_report_from_json(input_path)
        except (OSError, ValueError) as error:
            raise gdb.GdbError(f"Failed to read report from '{input_path}': {error}") from error
        if html_arg is not None:
            html_path = Path(html_arg).expanduser().resolve()
            try:
                file_size = dump_report_to_html(report, html_path)
            except OSError as error:
                raise gdb.GdbError(
                    f"Failed to write HTML report to '{html_path}': {error}"
                ) from error
            CONSOLE.print(f"[green]HTML report written to {html_path} ({file_size} bytes)[/green]")
            return
        render_report(report)


class _SecscanSubcommand(gdb.Command):
    """Base class for concrete ``secscan`` subcommands."""

    def __init__(self, parent: SecscanCmd, name: str) -> None:
        """Register one ``secscan <name>`` subcommand."""
        self.parent = parent
        self.name = name
        super().__init__(f"secscan {name}", gdb.COMMAND_USER)

    def _argv(self, arg: str) -> list[str]:
        """Split command arguments using GDB's CLI lexer."""
        return list(gdb.string_to_argv(arg)) if arg.strip() else []


class SecscanAuditCmd(_SecscanSubcommand):
    """Run the full security audit against the live target."""

    def __init__(self, parent: SecscanCmd) -> None:
        """Register the ``secscan audit`` subcommand."""
        super().__init__(parent, "audit")

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Execute ``secscan audit [<output.json>]``."""
        del from_tty
        self.parent._invoke_audit(self._argv(arg))


class SecscanReportCmd(_SecscanSubcommand):
    """Render a previously dumped audit report from a JSON file."""

    def __init__(self, parent: SecscanCmd) -> None:
        """Register the ``secscan report`` subcommand."""
        super().__init__(parent, "report")

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Execute ``secscan report <report.json>``."""
        del from_tty
        self.parent._invoke_report(self._argv(arg))


class SecscanHelpCmd(_SecscanSubcommand):
    """Show the secscan command reference."""

    def __init__(self, parent: SecscanCmd) -> None:
        """Register the ``secscan help`` subcommand."""
        super().__init__(parent, "help")

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Execute ``secscan help``."""
        del arg
        del from_tty
        render_help()
