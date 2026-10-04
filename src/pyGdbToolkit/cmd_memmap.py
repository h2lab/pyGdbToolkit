"""Architecture-neutral GDB memory-map command."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable, NoReturn

import gdb
from rich.console import Console
from rich.table import Table

from .arch.memmap import MemoryMapRegistry, MemoryRegion, RuntimeMemoryEvidence, TargetFingerprint
from .cmd_dap import DapCmd
from .cmd_svd import SvdSessionState
from .debug_port import DebugPortError
from .memmap import MemoryMapReport, probe_regions
from .memmap_runtime import (
    DEFAULT_MEMORY_MAP_REGISTRY,
    elf_regions,
    gdb_memory_context,
    gdb_execution_evidence,
    gdb_memory_policy,
    gdb_target_fingerprint,
    remote_memory_map,
    require_stopped_target,
    svd_regions,
)
from .session import SESSION, CommandHelp, CommandUsage, SessionSlice, ToolkitSession
from .svd import SvdDevice
from .target_memory import TargetMemory, TargetMemoryReader

CONSOLE = Console(force_terminal=True)
MEMMAP_HELP = CommandHelp(
    "memmap",
    "Discover declared memory regions and sample access through the current GDB core",
    (
        CommandUsage(
            "memmap discover [--vendor <manufacturer>] [--verify]",
            "Collect identity and maps; optionally verify declared endpoints",
        ),
        CommandUsage(
            "memmap show [--brief]", "Show detailed evidence or only the probable memory-map table"
        ),
        CommandUsage(
            "memmap bases [<manufacturer>]",
            "List family-specific reference windows, without target access",
        ),
        CommandUsage(
            "memmap probe --known [--stride <bytes>] [--max-reads <n>] [--timeout <seconds>]",
            "Sample server-declared memory, excluding MMIO and OTP",
        ),
        CommandUsage(
            "memmap probe --range <start>:<end> --allow-unsafe [--stride <bytes>] "
            "[--max-reads <n>] [--timeout <seconds>] [--ignore-memory-map]",
            "Explicitly accept potentially side-effecting reads of a half-open range",
        ),
        CommandUsage(
            "memmap report <output.json>", "Export declarations, context and observations"
        ),
        CommandUsage("memmap help", "Show command reference"),
    ),
    (
        "No target-memory writes, reset, unlock or automatic full-address-space scan.",
        "Reads use the selected GDB inferior/core, NOT the AP selected by dap select.",
        "Read errors are not proof of protection; successful reads do not prove write access.",
        "Sampling does not prove continuity, physical sizes, or absence of aliases.",
        "Manufacturer bases are indicative only and are never automatically probed.",
        "Defaults: stride 4096 bytes, 256 reads, 5 seconds checked between reads.",
        "A transport read can block beyond the budget; use GDB interrupt/transport timeout.",
        "Hardware fingerprinting reads documented CPUID/CoreSight/manufacturer metadata.",
    ),
)


@dataclass
class MemoryMapSessionState(SessionSlice):
    """Retain the latest map and its original access context."""

    report: MemoryMapReport | None = None
    svd_device: SvdDevice | None = None

    def reset(self) -> None:
        """Discard all target-specific declarations and measurements."""
        self.report = None
        self.svd_device = None


class _ArgumentParser(argparse.ArgumentParser):
    """Convert parser errors to GDB errors instead of exiting the interpreter."""

    def error(self, message: str) -> NoReturn:
        """Raise an exception for the command boundary to translate."""
        raise ValueError(message)


class MemmapCmd(gdb.Command):
    """Discover and probe memory with injectable architecture and access adapters."""

    HELP = MEMMAP_HELP

    def __init__(
        self,
        session: ToolkitSession = SESSION,
        registry: MemoryMapRegistry = DEFAULT_MEMORY_MAP_REGISTRY,
        dap: DapCmd | None = None,
        context: Callable[[], dict[str, Any]] = gdb_memory_context,
        memory: Callable[[], TargetMemory] = TargetMemoryReader,
        stopped: Callable[[], None] = require_stopped_target,
        fingerprint: Callable[[str, dict[str, Any] | None], TargetFingerprint | None] | None = None,
        execution: (
            Callable[[str, TargetFingerprint | None, list[MemoryRegion]], RuntimeMemoryEvidence]
            | None
        ) = None,
    ) -> None:
        """Register the prefix without performing target access at import time."""
        super().__init__("memmap", gdb.COMMAND_USER, gdb.COMPLETE_NONE, True)
        self.session = session
        self.registry = registry
        self.dap = dap
        self.context = context
        self.memory = memory
        self.stopped = stopped
        self.execution = (
            execution
            if execution is not None
            else (
                lambda architecture, fingerprint, regions: gdb_execution_evidence(
                    architecture, fingerprint, regions, self.registry
                )
            )
        )
        self.fingerprint = (
            fingerprint
            if fingerprint is not None
            else (
                lambda architecture, dap: gdb_target_fingerprint(architecture, dap, self.registry)
            )
        )
        session.register_command(self.HELP)

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Show help for the bare prefix."""
        del from_tty
        if arg.strip():
            raise gdb.GdbError("Unknown memmap subcommand. Use 'memmap help'.")
        self.run("help", [])

    def discover(self, vendor: str | None = None, verify: bool = False) -> MemoryMapReport:
        """Collect independent metadata sources, retaining partial failures."""
        context = self.context()
        report = MemoryMapReport(str(context["architecture"]), dict(context))
        self.session.publish_discovery(report, self.context)
        requested_vendor = vendor
        sources: tuple[tuple[str, Callable[[], list[MemoryRegion]]], ...] = (
            (
                "server",
                lambda: remote_memory_map(lambda command: gdb.execute(command, to_string=True)),
            ),
            ("ELF", lambda: elf_regions(gdb.execute("maintenance info sections", to_string=True))),
        )
        for name, collect in sources:
            try:
                report.regions.extend(collect())
            except (gdb.error, ValueError) as error:
                report.notes.append(f"{name} metadata unavailable: {error}")
        device = self.session.state(SvdSessionState).device
        if device is not None:
            try:
                report.regions.extend(svd_regions(device))
            except ValueError as error:
                report.notes.append(f"SVD metadata unavailable: {error}")
        else:
            report.notes.append("No SVD loaded; use svd load if available")
        if self.dap is not None:
            try:
                report.dap = self.dap.collect_report()
            except (gdb.error, DebugPortError) as error:
                report.notes.append(f"DAP metadata unavailable: {error}")
        report.fingerprint = self.fingerprint(report.architecture, report.dap)
        if report.fingerprint is not None:
            report.notes.append(f"Fingerprint confidence: {report.fingerprint.confidence}")
            if vendor is None and report.fingerprint.confidence != "conflict":
                vendor = report.fingerprint.vendor
        if vendor is not None:
            try:
                candidates = self.registry.baselines(vendor)
                if (
                    requested_vendor is not None
                    and report.fingerprint is not None
                    and report.fingerprint.confirmed
                ):
                    actual = self.registry.baselines(report.fingerprint.vendor)
                    if actual[0].vendor != candidates[0].vendor:
                        raise ValueError(
                            "Requested manufacturer conflicts with the confirmed fingerprint"
                        )
                report.candidates.extend(candidates)
            except ValueError as error:
                if requested_vendor is not None:
                    raise
                report.notes.append(str(error))
        execution = self.execution(report.architecture, report.fingerprint, report.regions)
        report.execution_hints.extend(execution.hints)
        report.regions.extend(execution.regions)
        report.notes.extend(execution.notes)
        report.candidates.extend(
            candidate for candidate in execution.candidates if candidate not in report.candidates
        )
        if not report.regions:
            report.notes.append(
                "Memory map unknown; no physical regions inferred from DAP or architecture"
            )
        report.notes.append(
            "Protection remains unknown unless supported by explicit device evidence"
        )
        state = self.session.state(MemoryMapSessionState)
        state.report = report
        state.svd_device = device
        self.session.publish_discovery(report, self._publication_context)
        if verify:
            self.stopped()
            points: list[MemoryRegion] = []
            for region in self._known_regions(report):
                first = (region.start + 3) & ~3
                last = (region.end - 4) & ~3
                if first <= last:
                    points.extend(
                        MemoryRegion(address, address + 4, region.kind, region.source)
                        for address in (first, last)
                    )
            probe_regions(report, self.memory(), points)
            report.probe_runs[-1]["gdb_memory_map_bypassed"] = False
            report.notes.append(
                "Verification samples declared endpoints only, not full-region coverage"
            )
        self.session.publish_discovery(report, self._publication_context)
        return report

    def _publication_context(self) -> dict[str, Any]:
        if (
            self.session.state(SvdSessionState).device
            is not self.session.state(MemoryMapSessionState).svd_device
        ):
            return {}
        return self.context()

    def _known_regions(self, report: MemoryMapReport) -> list[MemoryRegion]:
        excluded = [region for region in report.regions if region.kind in ("registers", "otp")]
        return [
            region
            for region in report.regions
            if region.probe_allowed
            and region.kind in ("ram", "rom", "flash")
            and not any(region.start < other.end and other.start < region.end for other in excluded)
        ]

    def _report(self) -> MemoryMapReport:
        report = self.session.state(MemoryMapSessionState).report
        if report is None:
            raise gdb.GdbError("No memory map collected; run 'memmap discover' first")
        return report

    def _probe(self, args: list[str]) -> None:
        parser = _ArgumentParser(prog="memmap probe", add_help=False)
        selection = parser.add_mutually_exclusive_group(required=True)
        selection.add_argument("--known", action="store_true")
        selection.add_argument("--range", dest="address_range")
        parser.add_argument("--allow-unsafe", action="store_true")
        parser.add_argument("--ignore-memory-map", action="store_true")
        parser.add_argument("--stride", type=lambda token: int(token, 0), default=4096)
        parser.add_argument("--max-reads", type=int, default=256)
        parser.add_argument("--timeout", type=float, default=5.0)
        options = parser.parse_args(args)
        if options.ignore_memory_map and (
            not options.allow_unsafe or options.address_range is None
        ):
            raise ValueError("--ignore-memory-map requires an explicit --range and --allow-unsafe")
        report = self._report()
        if (
            self.context() != report.context
            or self.session.state(SvdSessionState).device
            is not self.session.state(MemoryMapSessionState).svd_device
        ):
            raise gdb.GdbError(
                "Target/core/object/SVD context changed; run 'memmap discover' again"
            )
        self.stopped()
        if options.address_range is not None:
            if not options.allow_unsafe:
                raise ValueError(
                    "manual ranges require --allow-unsafe: reads may have side effects"
                )
            bounds = options.address_range.split(":")
            if len(bounds) != 2:
                raise ValueError("range must be <start>:<end> (end excluded)")
            start, end = (int(token, 0) for token in bounds)
            regions = [MemoryRegion(start, end, "unknown", "manual")]
            CONSOLE.print("Warning: explicit range reads may have side effects", markup=False)
        else:
            if options.allow_unsafe:
                raise ValueError("--allow-unsafe is only valid with --range")
            regions = self._known_regions(report)
            if not regions:
                raise ValueError(
                    "No eligible declared memory regions; use an explicit range if appropriate"
                )
        with gdb_memory_policy(options.ignore_memory_map):
            probe_regions(
                report,
                self.memory(),
                regions,
                stride=options.stride,
                max_reads=options.max_reads,
                timeout=options.timeout,
            )
        report.probe_runs[-1]["gdb_memory_map_bypassed"] = options.ignore_memory_map
        self.session.publish_discovery(report, self._publication_context)
        self.show(report)

    def show(self, report: MemoryMapReport, *, brief: bool = False) -> None:
        """Render declared ranges and point observations as separate tables."""
        if brief:
            self._show_brief(report)
            return
        CONSOLE.print(f"Memory view: {report.context}", markup=False)
        if report.fingerprint is not None:
            identity = report.fingerprint
            CONSOLE.print(
                f"Vendor: {identity.vendor} | SoC/family: {identity.soc} | "
                f"Confidence: {identity.confidence} | Server part (reported): {identity.server_soc or 'unknown'}",
                markup=False,
            )
            for evidence in identity.evidence:
                CONSOLE.print(evidence, markup=False)
        table = Table(title="Declared regions (ends excluded)")
        for column in (
            "Start",
            "End",
            "Kind",
            "Source",
            "Name",
            "Protection",
            "Access samples",
            "Address-space hint",
        ):
            table.add_column(
                column,
                no_wrap=column in ("Start", "End"),
                overflow="ignore" if column in ("Start", "End") else "fold",
            )
        provider = self.registry.provider(report.architecture)
        for region in sorted(report.regions, key=lambda entry: entry.start):
            table.add_row(
                hex(region.start),
                hex(region.end),
                region.kind,
                region.source,
                region.name,
                region.protection,
                self._access_summary(report, region.start, region.end),
                provider.describe(region.start, region.end),
            )
        CONSOLE.print(table)
        if report.execution_hints:
            hints = Table(title="Execution evidence (addresses, not inferred physical capacities)")
            for column in ("Origin", "Address", "Role", "Association", "Evidence"):
                hints.add_column(column, no_wrap=column == "Address")
            for hint in report.execution_assessments():
                associations = [
                    f"{region['name'] or region['kind']} {region['start']:#x}:{region['end']:#x}"
                    for region in hint["regions"]
                ]
                if not associations:
                    associations = [
                        f"candidate {candidate['start']:#x}:{candidate['end']:#x}"
                        for candidate in hint["candidates"]
                    ]
                hints.add_row(
                    hint["name"],
                    hex(hint["address"]),
                    hint["role"],
                    "; ".join(associations) or "unmapped-address",
                    hint["evidence"],
                )
            CONSOLE.print(hints)
        if report.candidates:
            candidates = Table(title="Manufacturer candidates (not discovered physical extents)")
            for column in ("Family", "Kind", "Start", "End", "Evidence", "Access samples"):
                candidates.add_column(column, no_wrap=column in ("Start", "End"))
            for assessment in report.candidate_assessments():
                candidates.add_row(
                    assessment["family"],
                    assessment["kind"],
                    hex(assessment["start"]),
                    hex(assessment["end"]),
                    assessment["status"],
                    f"{assessment['readable_points']} readable / {assessment['read_errors']} errors",
                )
            CONSOLE.print(candidates)
        if report.observations:
            samples = Table(title="Observed points (not whole-region coverage)")
            for column in ("Address", "Bytes", "Access", "Error"):
                samples.add_column(column)
            for observation in report.observations:
                samples.add_row(
                    hex(observation.address),
                    str(observation.size),
                    observation.status,
                    observation.error or "",
                )
            CONSOLE.print(samples)
        if report.probe_runs:
            last = report.probe_runs[-1]
            CONSOLE.print(
                f"Last probe: {last['reads']} reads; stop={last['stop_reason']}", markup=False
            )
        for note in report.notes:
            CONSOLE.print(note, markup=False)

    def _show_brief(self, report: MemoryMapReport) -> None:
        """Render probable ranges without presenting candidate bounds as physical sizes."""
        table = Table(
            title="Probable memory map (ends excluded; candidate bounds are not physical sizes)"
        )
        for column in ("Start", "End", "Kind", "Basis", "Access"):
            table.add_column(column, no_wrap=column in ("Start", "End"), overflow="fold")
        rows: list[tuple[int, int, str, str]] = [
            (region.start, region.end, region.kind, f"declared: {region.source}")
            for region in report.regions
        ]
        seen = {(start, end, kind) for start, end, kind, _ in rows}
        for candidate in report.candidate_assessments():
            key = (candidate["start"], candidate["end"], candidate["kind"])
            if candidate["status"] == "runtime-supported" and key not in seen:
                rows.append((*key, "candidate: " + ", ".join(candidate["execution_origins"])))
                seen.add(key)
        for start, end, kind, basis in sorted(rows):
            table.add_row(
                hex(start), hex(end), kind, basis, self._access_summary(report, start, end)
            )
        CONSOLE.print(table)

    def _access_summary(self, report: MemoryMapReport, start: int, end: int) -> str:
        samples = [sample for sample in report.observations if start <= sample.address < end]
        if not samples:
            return "not-probed"
        readable = sum(sample.status == "readable-sampled" for sample in samples)
        return f"{readable} readable / {len(samples) - readable} errors"

    def show_baselines(self, vendor: str | None = None) -> None:
        """Render family reference windows independently of the target snapshot."""
        entries = self.registry.baselines(vendor)
        table = Table(title="Manufacturer reference windows (not verified; ends excluded)")
        for column in ("Manufacturer", "Family / Architecture", "Kind", "Start", "End", "Basis"):
            table.add_column(
                column,
                no_wrap=column in ("Start", "End"),
                overflow="ignore" if column in ("Start", "End") else "fold",
            )
        for entry in sorted(
            entries, key=lambda baseline: (baseline.vendor, baseline.family, baseline.start)
        ):
            table.add_row(
                entry.vendor,
                f"{entry.family}\n{entry.architecture}",
                entry.kind,
                f"0x{entry.start:08X}",
                f"0x{entry.end:08X}",
                f"{entry.reference}: {entry.note}",
            )
        CONSOLE.print(table)
        CONSOLE.print(
            "Indicative family examples only, not a map of the connected device. "
            "Sizes, remapping, accessibility and protection require device evidence. "
            "These windows are not added to discover or probe --known.",
            markup=False,
        )

    def run(self, name: str, args: list[str]) -> None:
        """Dispatch validated arguments and translate portable failures."""
        try:
            if name == "help" and args:
                raise ValueError(f"Usage: memmap {name}")
            if name == "help":
                for entry in self.HELP.usage:
                    CONSOLE.print(f"{entry.syntax}: {entry.description}", markup=False)
                for note in self.HELP.notes:
                    CONSOLE.print(note, markup=False)
            elif name == "discover":
                parser = _ArgumentParser(prog="memmap discover", add_help=False)
                parser.add_argument("--vendor")
                parser.add_argument("--verify", action="store_true")
                options = parser.parse_args(args)
                self.show(self.discover(options.vendor, options.verify))
            elif name == "show":
                parser = _ArgumentParser(prog="memmap show", add_help=False)
                parser.add_argument("--brief", action="store_true")
                options = parser.parse_args(args)
                self.show(self._report(), brief=options.brief)
            elif name == "bases":
                if len(args) > 1:
                    raise ValueError("Usage: memmap bases [<manufacturer>]")
                self.show_baselines(args[0] if args else None)
            elif name == "probe":
                self._probe(args)
            elif name == "report":
                if len(args) != 1:
                    raise ValueError("Usage: memmap report <output.json>")
                path = Path(args[0]).expanduser()
                path.write_text(
                    json.dumps(self._report().to_dict(), indent=2) + "\n", encoding="utf-8"
                )
                CONSOLE.print(f"Report written to {path}", markup=False)
            else:
                raise ValueError("Unknown memmap subcommand. Use 'memmap help'.")
        except (ValueError, OSError) as error:
            raise gdb.GdbError(str(error)) from error


class MemmapSubcommand(gdb.Command):
    """Register a concrete subcommand sharing the portable dispatcher."""

    def __init__(self, parent: MemmapCmd, name: str) -> None:
        """Register one supported operation."""
        self.parent = parent
        self.name = name
        super().__init__(f"memmap {name}", gdb.COMMAND_USER)

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Parse quoted GDB arguments and invoke the parent command."""
        del from_tty
        self.parent.run(self.name, list(gdb.string_to_argv(arg)))
