"""The ``secrethunt`` GDB command for scanning and classifying secrets in ARM Cortex-M targets."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import shlex
from typing import TYPE_CHECKING, Any, Counter

if TYPE_CHECKING:
    import gdb
else:
    try:
        import gdb
    except ImportError:
        import unittest.mock as mock

        gdb = mock.MagicMock()

        class _DummyGdbCommand:
            def __init__(self, name: str, command_class: int) -> None:
                pass

        gdb.Command = _DummyGdbCommand
        gdb.COMMAND_USER = 0

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .target_memory import TargetMemoryReader, TargetReadError

CONSOLE = Console(force_terminal=True)

# Default memory regions for ARM Cortex-M architecture
CORTEX_M_REGIONS: dict[str, tuple[int, int]] = {
    "sram": (0x20000000, 0x20000),  # 128 KB SRAM
    "flash": (0x08000000, 0x40000),  # 256 KB Flash
    "backup": (0x40002800, 0x400),  # RTC / Backup registers
}


@dataclass
class Finding:
    """A high-entropy memory region candidate detected during scan."""

    address: int
    size: int
    entropy: float
    data: bytes
    classification: str = "High-Entropy Blob"
    likelihood: str = "Low"

    def to_dict(self) -> dict[str, Any]:
        """Convert the finding to a JSON-serializable dictionary.

        Returns
        -------
        dict[str, Any]
            Dictionary representation of the finding.
        """
        return {
            "address": f"0x{self.address:08X}",
            "size": self.size,
            "entropy": round(self.entropy, 3),
            "classification": self.classification,
            "likelihood": self.likelihood,
            "preview_hex": self.data[:32].hex(),
        }


@dataclass
class ScanSession:
    """State storage for scan results across command invocations."""

    findings: list[Finding] = field(default_factory=list)
    scanned_regions: list[str] = field(default_factory=list)
    threshold: float = 6.0
    window_size: int = 32


# Global session state for secrethunt
_SESSION = ScanSession()


def calculate_entropy(data: bytes) -> float:
    """Calculate normalized Shannon entropy of a byte sequence in bits per byte.

    Parameters
    ----------
    data
        Byte sequence to analyze.

    Returns
    -------
    float
        Normalized Shannon entropy value between 0.0 and 8.0.
    """
    if not data:
        return 0.0
    counter = Counter(data)
    total = len(data)
    entropy = 0.0
    for count in counter.values():
        p = count / total
        entropy -= p * math.log2(p)

    max_possible = min(8.0, math.log2(total))
    if max_possible > 0:
        return min(8.0, entropy * (8.0 / max_possible))
    return 0.0


def classify_finding(data: bytes, entropy: float) -> tuple[str, str]:
    """Classify a high-entropy byte block based on size, structure, and entropy.

    Parameters
    ----------
    data
        Raw bytes of the candidate finding.
    entropy
        Calculated Shannon entropy.

    Returns
    -------
    tuple[str, str]
        Tuple of ``(classification_label, likelihood)``.
    """
    size = len(data)

    if len(set(data)) <= 1:
        return ("Uniform Pattern", "None")

    if entropy >= 7.0:
        if size == 16:
            return ("AES-128 Key (Raw)", "High")
        if size == 32:
            return ("AES-256 / ECC-P256 Key Candidate", "High")
        if size in (24, 48, 66):
            return (f"ECC Key Candidate ({size * 8} bits)", "High")
        if size == 176:
            return ("AES-128 Expanded Key Schedule", "High")
        if size == 240:
            return ("AES-256 Expanded Key Schedule", "High")
        if size in (128, 256, 512):
            return (f"RSA Key Material ({size * 8} bits)", "Medium")
        return ("High-Entropy Secret / Token", "Medium")

    if entropy >= 6.0:
        if size == 16:
            return ("AES-128 Key Candidate", "Medium")
        if size == 32:
            return ("AES-256 / ECC Key Candidate", "Medium")
        if size in (176, 240):
            return ("AES Expanded Key Schedule Candidate", "Medium")
        return ("Medium-Entropy Data Blob", "Low")

    return ("Unclassified Data", "Low")


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping or contiguous address ranges.

    Parameters
    ----------
    ranges
        List of ``(start_address, end_address)`` tuples.

    Returns
    -------
    list[tuple[int, int]]
        Merged list of disjoint ``(start_address, end_address)`` tuples.
    """
    if not ranges:
        return []

    sorted_ranges = sorted(ranges, key=lambda r: r[0])
    merged: list[tuple[int, int]] = [sorted_ranges[0]]

    for current in sorted_ranges[1:]:
        prev_start, prev_end = merged[-1]
        if current[0] <= prev_end:
            merged[-1] = (prev_start, max(prev_end, current[1]))
        else:
            merged.append(current)

    return merged


def scan_memory_region(
    reader: TargetMemoryReader,
    base_address: int,
    size: int,
    threshold: float = 6.0,
    window_size: int = 32,
    step_size: int = 16,
) -> list[Finding]:
    """Scan a target memory region for high-entropy byte blocks.

    Parameters
    ----------
    reader
        Target memory reader instance.
    base_address
        Target base address.
    size
        Total size in bytes to scan.
    threshold
        Entropy threshold in bits per byte (default: 6.0).
    window_size
        Analysis window size in bytes (default: 32).
    step_size
        Sliding step size in bytes (default: 16).

    Returns
    -------
    list[Finding]
        List of detected high-entropy findings.
    """
    chunk_size = 4096
    offset = 0
    raw_ranges: list[tuple[int, int]] = []
    data_map: dict[int, bytes] = {}

    while offset < size:
        curr_addr = base_address + offset
        curr_read_size = min(chunk_size, size - offset)
        try:
            chunk = reader.read_bytes(curr_addr, curr_read_size)
            data_map[curr_addr] = chunk

            for win_offset in range(0, len(chunk) - window_size + 1, step_size):
                win_bytes = chunk[win_offset : win_offset + window_size]
                ent = calculate_entropy(win_bytes)
                if ent >= threshold:
                    win_start = curr_addr + win_offset
                    win_end = win_start + window_size
                    raw_ranges.append((win_start, win_end))
        except TargetReadError:
            pass
        offset += curr_read_size

    merged = merge_ranges(raw_ranges)
    findings: list[Finding] = []

    for start_addr, end_addr in merged:
        finding_size = end_addr - start_addr
        block_bytes = bytearray()
        chunk_offset = 0
        while chunk_offset < finding_size:
            addr = start_addr + chunk_offset
            chunk_base = (addr // chunk_size) * chunk_size
            in_chunk_offset = addr - chunk_base
            if chunk_base in data_map:
                available = len(data_map[chunk_base]) - in_chunk_offset
                needed = finding_size - chunk_offset
                to_take = min(available, needed)
                block_bytes.extend(
                    data_map[chunk_base][in_chunk_offset : in_chunk_offset + to_take]
                )
                chunk_offset += to_take
            else:
                break

        data = bytes(block_bytes)
        if data:
            ent = calculate_entropy(data)
            label, likelihood = classify_finding(data, ent)
            findings.append(
                Finding(
                    address=start_addr,
                    size=len(data),
                    entropy=ent,
                    data=data,
                    classification=label,
                    likelihood=likelihood,
                )
            )

    return findings


class SecretHuntCmd(gdb.Command):
    """Targeted entropy scanner for ARM Cortex-M secrets (SRAM, flash, backup registers)."""

    def __init__(self) -> None:
        """Register the command with GDB."""
        super().__init__("secrethunt", gdb.COMMAND_USER)

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Run the secrethunt command.

        Parameters
        ----------
        arg
            User-supplied argument string.
        from_tty
            Whether GDB invoked the command from terminal.

        Raises
        ------
        gdb.GdbError
            If invalid parameters or memory read errors occur.
        """
        del from_tty
        tokens = shlex.split(arg)
        if any(h in tokens for h in ("help", "-h", "--help")):
            self._handle_help()
            return

        parser = self._create_parser()

        try:
            args = parser.parse_args(tokens)
        except (argparse.ArgumentError, SystemExit) as error:
            raise gdb.GdbError(
                f"Invalid secrethunt arguments: {error}. Use 'secrethunt help' for usage."
            ) from error

        subcommand = args.subcommand
        if subcommand == "help":
            self._handle_help()
        elif subcommand is None or subcommand == "scan":
            self._handle_scan(args)
        elif subcommand == "classify":
            self._handle_classify(args)
        elif subcommand == "dump":
            self._handle_dump(args)

    def _create_parser(self) -> argparse.ArgumentParser:
        """Create the argument parser for secrethunt subcommands.

        Returns
        -------
        argparse.ArgumentParser
            Configured command-line parser.
        """
        parser = argparse.ArgumentParser(
            prog="secrethunt",
            description="Entropy scanner for secrets in ARM Cortex-M targets.",
            exit_on_error=False,
        )
        subparsers = parser.add_subparsers(dest="subcommand")

        # Scan subcommand
        scan_p = subparsers.add_parser("scan", help="Scan memory for high entropy regions")
        scan_p.add_argument(
            "target",
            nargs="*",
            default=["sram"],
            help="Region name (sram, flash, backup, all) or '0xADDR 0xSIZE'",
        )
        scan_p.add_argument(
            "--threshold",
            type=float,
            default=6.0,
            help="Entropy threshold in bits per byte (default: 6.0)",
        )
        scan_p.add_argument(
            "--window",
            type=int,
            default=32,
            help="Scan window size in bytes (default: 32)",
        )

        # Classify subcommand
        classify_p = subparsers.add_parser("classify", help="Classify high-entropy findings")
        classify_p.add_argument(
            "--crypto",
            action="store_true",
            help="Filter findings to show only cryptographic key candidates",
        )

        # Dump subcommand
        dump_p = subparsers.add_parser("dump", help="Export findings to JSON file")
        dump_p.add_argument("filename", type=str, help="Output JSON filename")

        # Help subcommand
        subparsers.add_parser("help", help="Display usage instructions for secrethunt")

        return parser

    def _handle_help(self) -> None:
        """Render usage information and subcommand help through Rich console."""
        usage_text = Text()
        usage_text.append(
            "secrethunt - Targeted entropy scanner for ARM Cortex-M targets\n\n",
            style="bold cyan",
        )
        usage_text.append("Usage:\n", style="bold yellow")
        usage_text.append(
            "  secrethunt scan [sram|flash|backup|all|0xADDR 0xSIZE] [--threshold N] [--window N]\n"
            "    Scan memory region(s) for high-entropy byte blocks.\n\n"
            "  secrethunt classify [--crypto]\n"
            "    Classify detected findings from the active scan session.\n\n"
            "  secrethunt dump <filename.json>\n"
            "    Export active scan findings to a JSON file.\n\n"
            "  secrethunt help | --help | -h\n"
            "    Display this usage information.\n\n"
        )
        usage_text.append("Examples:\n", style="bold yellow")
        usage_text.append(
            "  secrethunt scan sram\n"
            "  secrethunt scan 0x08000000 0x00100000 --threshold 7.0\n"
            "  secrethunt classify --crypto\n"
            "  secrethunt dump findings.json\n"
        )

        CONSOLE.print(
            Panel(
                usage_text,
                title="SecretHunt Usage & Help",
                box=box.SIMPLE_HEAVY,
            )
        )

    def _handle_scan(self, args: argparse.Namespace) -> None:
        """Handle the 'scan' subcommand execution.

        Parameters
        ----------
        args
            Parsed arguments for scan.
        """
        try:
            reader = TargetMemoryReader()
        except TargetReadError as err:
            raise gdb.GdbError(str(err)) from err

        targets: list[str] = getattr(args, "target", ["sram"]) or ["sram"]
        threshold: float = getattr(args, "threshold", 6.0)
        window: int = getattr(args, "window", 32)

        regions_to_scan: list[tuple[str, int, int]] = []

        if len(targets) == 2 and targets[0].startswith("0x") and targets[1].startswith("0x"):
            try:
                addr = int(targets[0], 0)
                sz = int(targets[1], 0)
                regions_to_scan.append((f"custom ({targets[0]})", addr, sz))
            except ValueError as err:
                raise gdb.GdbError(f"Invalid custom address/size: {targets}") from err
        else:
            region_name = targets[0].lower() if targets else "sram"
            if region_name == "all":
                for name, (addr, sz) in CORTEX_M_REGIONS.items():
                    regions_to_scan.append((name, addr, sz))
            elif region_name in CORTEX_M_REGIONS:
                addr, sz = CORTEX_M_REGIONS[region_name]
                regions_to_scan.append((region_name, addr, sz))
            else:
                regions_list = list(CORTEX_M_REGIONS.keys()) + ["all"]
                raise gdb.GdbError(f"Unknown region '{region_name}'. Choose from: {regions_list}")

        all_findings: list[Finding] = []
        scanned_names: list[str] = []

        for reg_name, base_addr, sz in regions_to_scan:
            findings = scan_memory_region(
                reader=reader,
                base_address=base_addr,
                size=sz,
                threshold=threshold,
                window_size=window,
            )
            all_findings.extend(findings)
            scanned_names.append(reg_name)

        _SESSION.findings = all_findings
        _SESSION.scanned_regions = scanned_names
        _SESSION.threshold = threshold
        _SESSION.window_size = window

        self._render_scan_summary(_SESSION)

    def _handle_classify(self, args: argparse.Namespace) -> None:
        """Handle the 'classify' subcommand execution.

        Parameters
        ----------
        args
            Parsed arguments for classify.
        """
        if not _SESSION.findings:
            self._handle_scan(
                argparse.Namespace(subcommand="scan", target=["sram"], threshold=6.0, window=32)
            )

        crypto_only: bool = getattr(args, "crypto", False)
        findings = _SESSION.findings

        if crypto_only:
            findings = [
                f
                for f in findings
                if "Key" in f.classification or "AES" in f.classification or f.likelihood == "High"
            ]

        self._render_classification_table(findings, crypto_only=crypto_only)

    def _handle_dump(self, args: argparse.Namespace) -> None:
        """Handle the 'dump' subcommand execution.

        Parameters
        ----------
        args
            Parsed arguments for dump.
        """
        if not _SESSION.findings:
            self._handle_scan(
                argparse.Namespace(subcommand="scan", target=["sram"], threshold=6.0, window=32)
            )

        filename: str = getattr(args, "filename", "findings.json")
        path = Path(filename)

        dump_data = {
            "scanned_regions": _SESSION.scanned_regions,
            "threshold": _SESSION.threshold,
            "window_size": _SESSION.window_size,
            "total_findings": len(_SESSION.findings),
            "findings": [f.to_dict() for f in _SESSION.findings],
        }

        try:
            with path.open("w", encoding="utf-8") as f:
                json.dump(dump_data, f, indent=2)
            CONSOLE.print(
                Panel(
                    f"[bold green]Dumped {len(_SESSION.findings)} finding(s) to '{path.resolve()}'[/bold green]",
                    title="SecretHunt Export",
                    box=box.SIMPLE_HEAVY,
                )
            )
        except OSError as err:
            raise gdb.GdbError(f"Failed to write dump file '{filename}': {err}") from err

    def _render_scan_summary(self, session: ScanSession) -> None:
        """Render scan results summary through Rich console.

        Parameters
        ----------
        session
            Active scan session data.
        """
        table = Table(
            title="SecretHunt Memory Scan Results",
            box=box.SIMPLE_HEAVY,
            header_style="bold cyan",
            show_header=True,
        )
        table.add_column("Region", style="bold", no_wrap=True)
        table.add_column("Scanned Range")
        table.add_column("High-Entropy Blocks", justify="right")

        regions_str = ", ".join(session.scanned_regions)
        table.add_row(
            regions_str, f"Threshold >= {session.threshold} bits/byte", str(len(session.findings))
        )

        CONSOLE.print(table)
        CONSOLE.print(
            Text(
                f"Scan complete. Found {len(session.findings)} high-entropy candidate block(s). "
                "Run 'secrethunt classify --crypto' for detailed analysis.",
                style="dim",
            )
        )

    def _render_classification_table(self, findings: list[Finding], crypto_only: bool) -> None:
        """Render classified findings table through Rich console.

        Parameters
        ----------
        findings
            List of findings to display.
        crypto_only
            Whether filtering was applied for crypto candidates.
        """
        title = (
            "SecretHunt Crypto Classification"
            if crypto_only
            else "SecretHunt All Findings Classification"
        )
        table = Table(
            title=title,
            box=box.SIMPLE_HEAVY,
            header_style="bold cyan",
            show_header=True,
        )
        table.add_column("Address", style="bold yellow", no_wrap=True)
        table.add_column("Size", justify="right")
        table.add_column("Entropy", justify="right")
        table.add_column("Classification", style="bold green")
        table.add_column("Likelihood")
        table.add_column("Preview (Hex)")

        if not findings:
            table.add_row("-", "-", "-", "No candidate secrets detected", "-", "-")
        else:
            for f in findings:
                ent_color = "green" if f.entropy >= 7.0 else "yellow"
                ent_text = Text(f"{f.entropy:.2f}", style=ent_color)
                like_color = (
                    "green"
                    if f.likelihood == "High"
                    else ("yellow" if f.likelihood == "Medium" else "dim")
                )
                like_text = Text(f.likelihood, style=like_color)
                preview = " ".join(f"{b:02x}" for b in f.data[:12])
                if len(f.data) > 12:
                    preview += " ..."

                table.add_row(
                    f"0x{f.address:08X}",
                    f"{f.size} B",
                    ent_text,
                    f.classification,
                    like_text,
                    preview,
                )

        CONSOLE.print(table)
