# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Portable ARMv8-A CPU identification from architected system registers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .target import AArch64TargetDescription

CPU_REGISTERS = (
    ("MIDR_EL1", 64),
    ("MPIDR_EL1", 64),
    ("REVIDR_EL1", 64),
    ("ID_AA64PFR0_EL1", 64),
    ("ID_AA64ISAR0_EL1", 64),
    ("ID_AA64MMFR0_EL1", 64),
    ("ID_AA64DFR0_EL1", 64),
    ("CTR_EL0", 64),
    ("DCZID_EL0", 64),
    ("CurrentEL", 64),
)

EXTERNAL_MIDR_OFFSET = 0xD00

_ARM_CORES = {
    0xD03: "Cortex-A53",
    0xD07: "Cortex-A57",
    0xD08: "Cortex-A72",
    0xD09: "Cortex-A73",
    0xD0A: "Cortex-A75",
    0xD0B: "Cortex-A76",
    0xD0C: "Neoverse N1",
    0xD0D: "Cortex-A77",
    0xD41: "Cortex-A78",
}


@dataclass(frozen=True)
class CpuRegister:
    """A named system register with no invented memory-mapped address."""

    name: str
    width_bits: int
    value: int | None
    valid_bits: int = 64
    source: str = "GDB register"

    @property
    def unavailable_reason(self) -> str | None:
        """Explain missing register access without assuming the hardware lacks it."""
        if self.value is None:
            return f"{self.name} is not exposed or readable through GDB"
        return None


class CpuRegisterReader(Protocol):
    """Read a named CPU register while retaining source and partial-width evidence."""

    def read_register(self, name: str, width_bits: int) -> CpuRegister:
        """Return the available bits or explicit register unavailability."""
        ...


@dataclass(frozen=True)
class CpuIdentity:
    """CPU designer, part, and revision decoded from MIDR_EL1."""

    implementer: int
    variant: int
    architecture: int
    part_number: int
    revision: int

    @property
    def core_name(self) -> str:
        """Name catalogued Arm cores and preserve unknown implementer/part pairs."""
        if self.implementer == 0x41 and self.architecture == 0xF:
            return _ARM_CORES.get(self.part_number, f"Unknown Arm part 0x{self.part_number:03X}")
        return f"Unknown CPU (implementer 0x{self.implementer:02X}, part 0x{self.part_number:03X})"

    @property
    def implementer_name(self) -> str:
        """Report the CPU IP designer, not the surrounding SoC manufacturer."""
        return "Arm" if self.implementer == 0x41 else f"Unknown (0x{self.implementer:02X})"

    @property
    def rnp_revision(self) -> str:
        """Return Arm's rNp revision notation."""
        return f"r{self.variant}p{self.revision}"


@dataclass(frozen=True)
class CpuReport:
    """An AArch64 CPU report retaining every register and its availability."""

    target: AArch64TargetDescription
    registers: tuple[CpuRegister, ...]
    identity: CpuIdentity | None

    def register(self, name: str) -> CpuRegister:
        """Find a collected register by its architected name."""
        return next(register for register in self.registers if register.name == name)


def decode_midr(value: int) -> CpuIdentity:
    """Decode MIDR_EL1's defined low 32 bits independently of SoC topology."""
    return CpuIdentity(
        implementer=(value >> 24) & 0xFF,
        variant=(value >> 20) & 0xF,
        architecture=(value >> 16) & 0xF,
        part_number=(value >> 4) & 0xFFF,
        revision=value & 0xF,
    )


def collect_cpu_report(target: AArch64TargetDescription, reader: CpuRegisterReader) -> CpuReport:
    """Read named registers only, without executing instructions or changing selectors."""
    registers = [reader.read_register(name, width_bits) for name, width_bits in CPU_REGISTERS]
    midr = registers[0].value
    return CpuReport(target, tuple(registers), None if midr is None else decode_midr(midr))


def affinity(value: int) -> tuple[int, int, int, int]:
    """Return MPIDR_EL1 Aff3:Aff2:Aff1:Aff0, not a physical core count."""
    return ((value >> 32) & 0xFF, (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def physical_address_bits(value: int) -> int | None:
    """Decode ID_AA64MMFR0_EL1.PARange, leaving reserved encodings unknown."""
    return {0: 32, 1: 36, 2: 40, 3: 42, 4: 44, 5: 48, 6: 52, 7: 56}.get(value & 0xF)


def simd_support(value: int, shift: int) -> str:
    """Decode PFR0 FP/AdvSIMD fields without treating reserved encodings as support."""
    return {0: "Supported", 1: "Supported, including FP16", 15: "Not implemented"}.get(
        (value >> shift) & 0xF, "Unknown encoding"
    )
