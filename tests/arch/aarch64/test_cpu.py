# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Tests for portable AArch64 CPU register collection and decoding."""

from __future__ import annotations

import pytest

from pyGdbToolkit.arch import Architecture
from pyGdbToolkit.arch.aarch64.cpu import (
    CPU_REGISTERS,
    CpuRegister,
    affinity,
    collect_cpu_report,
    decode_midr,
    physical_address_bits,
    simd_support,
)
from pyGdbToolkit.arch.aarch64.target import AArch64TargetDescription


class RegisterReader:
    """Return configured named registers and record each requested alias set."""

    def __init__(self, values: dict[str, int]) -> None:
        """Initialize a fake system-register source."""
        self.values = values
        self.calls: list[tuple[str, ...]] = []

    def read_first(self, names: tuple[str, ...]) -> int | None:
        """Supply a register value without memory access."""
        self.calls.append(names)
        return next((self.values[name] for name in names if name in self.values), None)

    def read_register(self, name: str, width_bits: int) -> CpuRegister:
        """Expose the collector contract with full-width register evidence."""
        value = self.read_first((name.lower(), name))
        return CpuRegister(
            name, width_bits, None if value is None else value & ((1 << width_bits) - 1)
        )


def target() -> AArch64TargetDescription:
    """Build the minimal metadata target returned by the architecture probe."""
    return AArch64TargetDescription(Architecture.AARCH64, "Arm", "AArch64", "unknown", "aarch64")


def test_midr_identifies_cortex_a53() -> None:
    """MIDR, not the configured SoC name, identifies the CPU and revision."""
    identity = decode_midr(0x410FD034)
    assert identity.core_name == "Cortex-A53"
    assert identity.implementer_name == "Arm"
    assert identity.rnp_revision == "r0p4"
    assert identity.part_number == 0xD03


@pytest.mark.parametrize("midr", (0x420FD034, 0x410F1234, 0x4100D034))
def test_unknown_midr_is_not_guessed_from_part_alone(midr: int) -> None:
    """Part numbers require the correct implementer and MIDR architecture field."""
    assert "Unknown" in decode_midr(midr).core_name


def test_collection_keeps_missing_registers_and_unsigned_widths() -> None:
    """Missing values stay unavailable and GDB signed integers are masked to register width."""
    reader = RegisterReader({"MIDR_EL1": 0x410FD034, "mpidr_el1": -1})
    report = collect_cpu_report(target(), reader)
    assert report.identity is not None
    assert report.identity.core_name == "Cortex-A53"
    assert report.register("MPIDR_EL1").value == 0xFFFFFFFFFFFFFFFF
    assert report.register("REVIDR_EL1").unavailable_reason is not None
    assert reader.calls == [(name.lower(), name) for name, _ in CPU_REGISTERS]


def test_missing_midr_preserves_metadata_without_inventing_identity() -> None:
    """A connected AArch64 target remains reportable when its server hides system registers."""
    report = collect_cpu_report(target(), RegisterReader({}))
    assert report.target.architecture is Architecture.AARCH64
    assert report.identity is None
    assert all(register.value is None for register in report.registers)


def test_mpidr_affinity_retains_all_four_levels() -> None:
    """MPIDR affinity is not truncated to 32 bits or interpreted as a CPU count."""
    assert affinity(0x1280345678) == (0x12, 0x34, 0x56, 0x78)


@pytest.mark.parametrize("encoding,bits", ((0, 32), (2, 40), (5, 48), (6, 52), (15, None)))
def test_physical_address_width(encoding: int, bits: int | None) -> None:
    """Architected PARange encodings distinguish physical width from RAM capacity."""
    assert physical_address_bits(encoding) == bits


@pytest.mark.parametrize(
    "encoding,result",
    (
        (0, "Supported"),
        (1, "Supported, including FP16"),
        (15, "Not implemented"),
        (2, "Unknown encoding"),
    ),
)
def test_fp_and_simd_encodings(encoding: int, result: str) -> None:
    """Negative and reserved PFR0 feature encodings must not imply support."""
    assert simd_support(encoding << 16, 16) == result
