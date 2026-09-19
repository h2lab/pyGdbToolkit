"""Tests for standard Arm CPUID decoding."""

from __future__ import annotations

import pytest

from pyGdbToolkit.cpuid import decode_cpuid


def test_decodes_cortex_m4_and_rnp_revision() -> None:
    """Standard CPUID fields identify Cortex-M4 and its rNp revision."""
    cpuid = decode_cpuid(0x413FC247)

    assert cpuid.implementer_name == "Arm"
    assert cpuid.architecture == 0xF
    assert cpuid.core == "Cortex-M4"
    assert cpuid.rnp_revision == "r3p7"


def test_unknown_part_and_implementer_are_retained() -> None:
    """Unknown CPUID values retain their raw, useful information."""
    cpuid = decode_cpuid(0x991F1234)

    assert cpuid.implementer_name == "Unknown (0x99)"
    assert cpuid.core == "Unknown Cortex-M part 0x123"


@pytest.mark.parametrize(
    ("part_number", "name"),
    (
        (0xD23, "Cortex-M85"),
        (0xD32, "Cortex-M52"),
    ),
)
def test_decodes_newer_cortex_m_parts(part_number: int, name: str) -> None:
    """Newer Cortex-M CPUID part numbers resolve to their correct core names."""
    cpuid = decode_cpuid(0x410F0000 | (part_number << 4))

    assert cpuid.core == name


@pytest.mark.parametrize("value", (-1, 0x1_0000_0000))
def test_rejects_non_32_bit_values(value: int) -> None:
    """Values outside the architected register width are invalid."""
    with pytest.raises(ValueError, match="unsigned 32-bit"):
        decode_cpuid(value)
