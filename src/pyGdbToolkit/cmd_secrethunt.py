"""The ``secrethunt`` GDB command for scanning and classifying secrets in ARM Cortex-M targets."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import shlex
import struct
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

# Default memory region constants removed in favor of explicit configuration via 'secrethunt set'


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
    """State storage for scan results and configured memory regions across command invocations."""

    findings: list[Finding] = field(default_factory=list)
    scanned_regions: list[str] = field(default_factory=list)
    threshold: float = 6.0
    window_size: int = 32
    configured_regions: dict[str, tuple[int, int]] = field(default_factory=dict)


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


# fmt: off
# Exception de formatage pour simplifier la lisibilité du fichier (alignement 20 valeurs/ligne)
_AES_SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76, 0xCA, 0x82, 0xC9, 0x7D,  # noqa: E501
    0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0, 0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC,  # noqa: E501
    0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15, 0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2,  # noqa: E501
    0xEB, 0x27, 0xB2, 0x75, 0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,  # noqa: E501
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF, 0xD0, 0xEF, 0xAA, 0xFB,  # noqa: E501
    0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8, 0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5,  # noqa: E501
    0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2, 0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D,  # noqa: E501
    0x64, 0x5D, 0x19, 0x73, 0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,  # noqa: E501
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79, 0xE7, 0xC8, 0x37, 0x6D,  # noqa: E501
    0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08, 0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6,  # noqa: E501
    0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A, 0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9,  # noqa: E501
    0x86, 0xC1, 0x1D, 0x9E, 0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,  # noqa: E501
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,  # noqa: E501
)
# fmt: on

_AES_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)

_SHA256_H0_BE = b"\x6a\x09\xe6\x67\xbb\x67\xae\x85\x3c\x6e\xf3\x72\xa5\x4f\xf5\x3a"
_SHA256_H0_LE = b"\x67\xe6\x09\x6a\x85\xae\x67\xbb\x72\xf3\x6e\x3c\x3a\xf5\x4f\xa5"
_SHA1_H0_BE = b"\x67\x45\x23\x01\xef\xcd\xab\x89\x98\xba\xdc\xfe\x10\x32\x54\x76\xc3\xd2\xe1\xf0"
_SHA256_K_BE = b"\x42\x8a\x2f\x98\x71\x37\x44\x91\xb5\xc0\xfb\xcf\xe9\xb5\xd2\x4a"

# ARM Thumb / Thumb-2 opcode pattern dictionary: category -> (value, mask)
THUMB_OPCODE_DICT: dict[str, tuple[int, int]] = {
    "Shift (LSL, LSR, ASR)": (0x0000, 0xE000),
    "Add / Subtract": (0x1800, 0xF800),
    "Move / Compare / Add / Sub Immediate": (0x2000, 0xE000),
    "Data Processing Register": (0x4000, 0xFC00),
    "Special Data / Branch Exchange (BX, BLX)": (0x4400, 0xFC00),
    "Load PC-Relative (LDR Literal)": (0x4800, 0xF800),
    "Load / Store Register Offset": (0x5000, 0xF000),
    "Load / Store Word / Byte Immediate": (0x6000, 0xE000),
    "Load / Store Halfword": (0x8000, 0xF000),
    "Load / Store SP-Relative": (0x9000, 0xF000),
    "Generate Address (ADD PC/SP)": (0xA000, 0xF000),
    "Misc / Stack (PUSH, POP, BKPT, CPS, NOP, IT, CBZ, CBNZ)": (0xB000, 0xF000),
    "Load / Store Multiple (LDM, STM)": (0xC000, 0xF000),
    "Conditional Branch / SVC": (0xD000, 0xF000),
    "Unconditional Branch (B)": (0xE000, 0xF800),
    "Thumb-2 32-bit Load / Store Prefix": (0xE800, 0xF800),
    "Thumb-2 32-bit Data Processing / Branch Prefix": (0xF000, 0xF800),
    "Thumb-2 32-bit Memory / Hints Prefix": (0xF800, 0xF800),
}


def _is_thumb_target() -> bool:
    """Check if the target selected frame or CPU uses ARM Thumb / Thumb-2 ISA."""
    try:
        frame = gdb.selected_frame()
        arch = frame.architecture().name().lower()
        return any(k in arch for k in ("arm", "cortex-m", "thumb"))
    except Exception:
        return True


def _is_thumb_instruction_sequence(data: bytes) -> bool:
    """Check if a byte sequence consists of ARM Thumb / Thumb-2 code instructions using THUMB_OPCODE_DICT.

    Parameters
    ----------
    data
        Raw bytes to analyze.

    Returns
    -------
    bool
        True if the data matches Thumb / Thumb-2 instruction patterns.
    """
    if len(data) < 4 or len(data) % 2 != 0:
        return False

    valid_instructions = 0
    total_instructions = 0
    i = 0

    while i < len(data) - 1:
        hw1 = data[i] | (data[i + 1] << 8)
        total_instructions += 1

        if (hw1 & 0xF800) in (0xE800, 0xF000, 0xF800):
            if i + 3 < len(data):
                hw2 = data[i + 2] | (data[i + 3] << 8)
                if (hw1 & 0xF800) == 0xF000 and (hw1 & 0x0800) == 0x0800:
                    valid = (hw2 & 0x8000) != 0
                else:
                    valid = True

                if valid:
                    valid_instructions += 1
                i += 4
            else:
                break
        else:
            matched = False
            for _name, (val, mask) in THUMB_OPCODE_DICT.items():
                if (hw1 & mask) == val:
                    matched = True
                    break
            if matched:
                valid_instructions += 1
            i += 2

    if total_instructions == 0:
        return False

    return (valid_instructions / total_instructions) >= 0.85


def _is_aes128_key_schedule(data: bytes) -> bool:
    """Check whether a 176-byte block matches the mathematical AES-128 key expansion."""
    if len(data) != 176:
        return False

    for endian in ("<", ">"):
        words = struct.unpack(f"{endian}44I", data)
        valid = True
        for i in range(4, 44):
            prev = words[i - 1]
            if i % 4 == 0:
                b0 = (prev >> 24) & 0xFF
                b1 = (prev >> 16) & 0xFF
                b2 = (prev >> 8) & 0xFF
                b3 = prev & 0xFF
                rot_sub = (
                    (_AES_SBOX[b1] << 24)
                    | (_AES_SBOX[b2] << 16)
                    | (_AES_SBOX[b3] << 8)
                    | _AES_SBOX[b0]
                )
                rcon_val = _AES_RCON[i // 4 - 1] << 24
                expected = words[i - 4] ^ rot_sub ^ rcon_val
            else:
                expected = words[i - 4] ^ prev

            if words[i] != expected:
                valid = False
                break
        if valid:
            return True
    return False


def _is_aes256_key_schedule(data: bytes) -> bool:
    """Check whether a 240-byte block matches the mathematical AES-256 key expansion."""
    if len(data) != 240:
        return False

    for endian in ("<", ">"):
        words = struct.unpack(f"{endian}60I", data)
        valid = True
        for i in range(8, 60):
            prev = words[i - 1]
            b0 = (prev >> 24) & 0xFF
            b1 = (prev >> 16) & 0xFF
            b2 = (prev >> 8) & 0xFF
            b3 = prev & 0xFF

            if i % 8 == 0:
                rot_sub = (
                    (_AES_SBOX[b1] << 24)
                    | (_AES_SBOX[b2] << 16)
                    | (_AES_SBOX[b3] << 8)
                    | _AES_SBOX[b0]
                )
                rcon_val = _AES_RCON[i // 8 - 1] << 24
                expected = words[i - 8] ^ rot_sub ^ rcon_val
            elif i % 8 == 4:
                sub = (
                    (_AES_SBOX[b0] << 24)
                    | (_AES_SBOX[b1] << 16)
                    | (_AES_SBOX[b2] << 8)
                    | _AES_SBOX[b3]
                )
                expected = words[i - 8] ^ sub
            else:
                expected = words[i - 8] ^ prev

            if words[i] != expected:
                valid = False
                break
        if valid:
            return True
    return False


def _is_sha256_initial_state(data: bytes) -> bool:
    """Check if data contains SHA-256 initial hash values (H0)."""
    return _SHA256_H0_BE in data or _SHA256_H0_LE in data


def _has_sha256_constants(data: bytes) -> bool:
    """Check if data contains SHA-256 round constants (K table)."""
    return _SHA256_K_BE in data or b"\x98\x2f\x8a\x42\x91\x44\x37\x71" in data


def _is_ascii_literal_string(data: bytes) -> bool:
    """Check if a byte sequence consists of printable ASCII string literal(s) ending with null byte(s)."""
    if not data or not data.endswith(b"\x00"):
        return False
    non_null_bytes = [b for b in data if b != 0]
    if not non_null_bytes:
        return False
    printable = set(range(0x20, 0x7F)) | {0x09, 0x0A, 0x0D, 0x00}
    return all(b in printable for b in data)


def classify_finding(
    data: bytes, entropy: float, is_flash: bool = False, is_thumb: bool = True
) -> tuple[str, str]:
    """Classify a high-entropy byte block based on size, structure, and entropy.

    Parameters
    ----------
    data
        Raw bytes of the candidate finding.
    entropy
        Calculated Shannon entropy.
    is_flash
        Whether the finding was located in Flash memory.
    is_thumb
        Whether the target architecture uses Thumb / Thumb-2 ISA.

    Returns
    -------
    tuple[str, str]
        Tuple of ``(classification_label, likelihood)``.
    """
    size = len(data)

    if len(set(data)) <= 1:
        return ("Uniform Pattern", "None")

    if _is_ascii_literal_string(data):
        return ("ASCII String Literal", "None")

    if is_flash and is_thumb and _is_thumb_instruction_sequence(data):
        return ("Thumb-2 Instruction Block", "None")

    if _is_sha256_initial_state(data):
        return ("SHA-256 Initial State / H Vector", "High")

    if _has_sha256_constants(data):
        return ("SHA-256 Round Constants (K Table)", "High")

    if _SHA1_H0_BE in data:
        return ("SHA-1 Initial State / H Vector", "High")

    if size == 176 and _is_aes128_key_schedule(data):
        return ("Verified AES-128 Expanded Key Schedule", "High")

    if size == 240 and _is_aes256_key_schedule(data):
        return ("Verified AES-256 Expanded Key Schedule", "High")

    if entropy >= 7.0:
        if size == 16:
            return ("AES-128 Key / MD5 Digest (128 bits)", "High")
        if size == 20:
            return ("SHA-1 / HMAC-SHA1 Digest Candidate (160 bits)", "High")
        if size == 28:
            return ("SHA-224 Digest Candidate (224 bits)", "High")
        if size == 32:
            return ("AES-256 Key / SHA-256 Digest / ECC-P256 Candidate", "High")
        if size == 48:
            return ("SHA-384 Digest / ECC-P384 Key Candidate (384 bits)", "High")
        if size == 64:
            return ("SHA-512 Digest / HMAC Block Candidate (512 bits)", "High")
        if size in (24, 66):
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
            return ("AES-128 Key / MD5 Candidate", "Medium")
        if size == 20:
            return ("SHA-1 Digest Candidate (160 bits)", "Medium")
        if size == 28:
            return ("SHA-224 Digest Candidate (224 bits)", "Medium")
        if size == 32:
            return ("AES-256 Key / SHA-256 Digest Candidate", "Medium")
        if size == 48:
            return ("SHA-384 Digest Candidate (384 bits)", "Medium")
        if size == 64:
            return ("SHA-512 Digest Candidate (512 bits)", "Medium")
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
    is_flash: bool = False,
    is_thumb: bool = True,
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
    is_flash
        Whether the region being scanned is Flash memory.
    is_thumb
        Whether the target uses ARM Thumb / Thumb-2 ISA.

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
            if is_flash and is_thumb and _is_thumb_instruction_sequence(data):
                continue
            ent = calculate_entropy(data)
            label, likelihood = classify_finding(data, ent, is_flash=is_flash, is_thumb=is_thumb)
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
        elif subcommand == "set":
            self._handle_set(args)
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

        # Set subcommand
        set_p = subparsers.add_parser("set", help="Set base address and size for a memory region")
        set_p.add_argument("region", choices=["sram", "flash", "backup"], help="Region name")
        set_p.add_argument("address", type=str, help="Base address (e.g., 0x20000000)")
        set_p.add_argument("size", type=str, help="Region size in bytes (e.g., 0x20000)")

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
            "  secrethunt set [sram|flash|backup] <address> <size>\n"
            "    Configure the memory address range for a region (e.g., 'secrethunt set sram 0x20000000 0x20000').\n\n"
            "  secrethunt scan [sram|flash|backup|all|0xADDR 0xSIZE] [--threshold N] [--window N]\n"
            "    Scan configured region(s) for high-entropy byte blocks.\n\n"
            "  secrethunt classify [--crypto]\n"
            "    Classify detected findings from the active scan session.\n\n"
            "  secrethunt dump <filename.json>\n"
            "    Export active scan findings to a JSON file.\n\n"
            "  secrethunt help | --help | -h\n"
            "    Display this usage information.\n\n"
        )
        usage_text.append("Examples:\n", style="bold yellow")
        usage_text.append(
            "  secrethunt set sram 0x20000000 0x20000\n"
            "  secrethunt set flash 0x08000000 0x40000\n"
            "  secrethunt scan sram\n"
            "  secrethunt scan flash --threshold 7.0\n"
            "  secrethunt scan all\n"
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

    def _handle_set(self, args: argparse.Namespace) -> None:
        """Handle the 'set' subcommand execution.

        Parameters
        ----------
        args
            Parsed arguments for set.
        """
        region: str = args.region.lower()
        try:
            addr = int(args.address, 0)
            sz = int(args.size, 0)
            if addr < 0:
                raise gdb.GdbError("Base address must not be negative")
            if sz <= 0:
                raise gdb.GdbError("Region size must be positive")
        except ValueError as err:
            raise gdb.GdbError(f"Invalid address or size: '{args.address}' '{args.size}'") from err

        _SESSION.configured_regions[region] = (addr, sz)

        CONSOLE.print(
            Panel(
                f"[bold green]Set region '{region}' to 0x{addr:08X} - 0x{addr + sz:08X} ({sz} bytes)[/bold green]",
                title="SecretHunt Region Configured",
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
                requested = ["sram", "flash", "backup"]
            elif region_name in ("sram", "flash", "backup"):
                requested = [region_name]
            else:
                raise gdb.GdbError(
                    f"Unknown region '{region_name}'. Choose from: sram, flash, backup, all or '0xADDR 0xSIZE'."
                )

            unconfigured: list[str] = []
            for name in requested:
                if name in _SESSION.configured_regions:
                    addr, sz = _SESSION.configured_regions[name]
                    regions_to_scan.append((name, addr, sz))
                else:
                    unconfigured.append(name)

            if unconfigured:
                unconf_str = ", ".join(unconfigured)
                raise gdb.GdbError(
                    f"Region(s) {unconf_str} not configured. Use 'secrethunt set <region> <address> <size>' before scanning."
                )

        all_findings: list[Finding] = []
        scanned_names: list[str] = []
        is_thumb = _is_thumb_target()

        for reg_name, base_addr, sz in regions_to_scan:
            is_flash = reg_name.lower() == "flash" or (0x08000000 <= base_addr < 0x20000000)
            findings = scan_memory_region(
                reader=reader,
                base_address=base_addr,
                size=sz,
                threshold=threshold,
                window_size=window,
                is_flash=is_flash,
                is_thumb=is_thumb,
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
                if "Key" in f.classification
                or "AES" in f.classification
                or "SHA" in f.classification
                or "MD5" in f.classification
                or "ECC" in f.classification
                or "RSA" in f.classification
                or f.likelihood == "High"
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
