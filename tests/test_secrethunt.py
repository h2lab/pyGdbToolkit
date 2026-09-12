"""Tests for the secrethunt GDB command and entropy scanner."""

from __future__ import annotations

import math
import os
import tempfile
from unittest.mock import MagicMock

from pyGdbToolkit.cmd_secrethunt import (
    Finding,
    SecretHuntCmd,
    calculate_entropy,
    classify_finding,
    merge_ranges,
    scan_memory_region,
)
from pyGdbToolkit.target_memory import TargetMemoryReader, TargetReadError


def test_calculate_entropy_empty() -> None:
    """Test entropy calculation for empty bytes."""
    assert calculate_entropy(b"") == 0.0


def test_calculate_entropy_uniform() -> None:
    """Test entropy calculation for uniform bytes."""
    assert calculate_entropy(b"\xaa" * 32) == 0.0


def test_calculate_entropy_high() -> None:
    """Test entropy calculation for random bytes."""
    random_bytes = os.urandom(256)
    entropy = calculate_entropy(random_bytes)
    assert entropy > 7.0


def test_classify_finding_aes128() -> None:
    """Test classification for 16-byte high-entropy block (AES-128)."""
    data = os.urandom(16)
    label, likelihood = classify_finding(data, entropy=7.5)
    assert label == "AES-128 Key (Raw)"
    assert likelihood == "High"


def test_classify_finding_aes256() -> None:
    """Test classification for 32-byte high-entropy block (AES-256 / ECC-P256)."""
    data = os.urandom(32)
    label, likelihood = classify_finding(data, entropy=7.5)
    assert "AES-256" in label
    assert likelihood == "High"


def test_classify_finding_key_schedule() -> None:
    """Test classification for AES-128 key schedule (176 bytes)."""
    data = os.urandom(176)
    label, likelihood = classify_finding(data, entropy=7.2)
    assert label == "AES-128 Expanded Key Schedule"
    assert likelihood == "High"


def test_classify_finding_uniform() -> None:
    """Test classification for uniform byte pattern."""
    data = b"\x00" * 32
    label, likelihood = classify_finding(data, entropy=0.0)
    assert label == "Uniform Pattern"
    assert likelihood == "None"


def test_merge_ranges() -> None:
    """Test merging overlapping and adjacent ranges."""
    ranges = [(0x1000, 0x1020), (0x1010, 0x1030), (0x2000, 0x2010)]
    merged = merge_ranges(ranges)
    assert merged == [(0x1000, 0x1030), (0x2000, 0x2010)]


def test_finding_to_dict() -> None:
    """Test finding dictionary serialization."""
    f = Finding(
        address=0x20000000,
        size=16,
        entropy=7.54321,
        data=b"\x01" * 16,
        classification="AES-128 Key (Raw)",
        likelihood="High",
    )
    d = f.to_dict()
    assert d["address"] == "0x20000000"
    assert d["size"] == 16
    assert d["entropy"] == 7.543
    assert d["classification"] == "AES-128 Key (Raw)"
    assert d["likelihood"] == "High"


def test_scan_memory_region_mock() -> None:
    """Test memory scanning with mock reader."""
    mock_reader = MagicMock(spec=TargetMemoryReader)
    high_entropy_data = os.urandom(64)
    low_entropy_data = b"\x00" * 64
    memory = low_entropy_data + high_entropy_data + low_entropy_data

    def mock_read_bytes(addr: int, size: int) -> bytes:
        offset = addr - 0x20000000
        return memory[offset : offset + size]

    mock_reader.read_bytes.side_effect = mock_read_bytes

    findings = scan_memory_region(
        reader=mock_reader,
        base_address=0x20000000,
        size=len(memory),
        threshold=6.0,
        window_size=32,
        step_size=16,
    )

    assert len(findings) >= 1
    assert findings[0].address >= 0x20000040


def test_secrethunt_cmd_help() -> None:
    """Test calling secrethunt help displays help without error."""
    cmd = SecretHuntCmd()
    cmd.invoke("help", from_tty=True)
    cmd.invoke("--help", from_tty=True)
    cmd.invoke("-h", from_tty=True)
