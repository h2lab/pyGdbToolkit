# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Portable architecture target-description and register-reading contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ..target_memory import TargetMemory


class Architecture(StrEnum):
    """Architectures supported by the target-description registry."""

    ARM = "arm"
    RISCV = "riscv"
    XTENSA = "xtensa"


@dataclass(frozen=True)
class TargetDescription:
    """Architecture-neutral identity for an inspected target."""

    architecture: Architecture
    family: str
    core_name: str
    revision: str


@dataclass(frozen=True)
class RegisterValue:
    """A system-register value or the explicit reason it could not be read."""

    name: str
    address: int
    width_bits: int
    value: int | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        """Validate that a register result has exactly one value state."""
        if (self.value is None) == (self.unavailable_reason is None):
            raise ValueError("a register value must be available or unavailable")
        if self.address < 0:
            raise ValueError("a register address must not be negative")
        if self.width_bits <= 0:
            raise ValueError("a register width must be positive")

    @classmethod
    def known(cls, name: str, address: int, width_bits: int, value: int) -> RegisterValue:
        """Create an available register value."""
        return cls(name=name, address=address, width_bits=width_bits, value=value)

    @classmethod
    def unavailable(
        cls,
        name: str,
        address: int,
        width_bits: int,
        reason: str,
    ) -> RegisterValue:
        """Create an unavailable register result."""
        return cls(
            name=name,
            address=address,
            width_bits=width_bits,
            unavailable_reason=reason,
        )

    @property
    def is_available(self) -> bool:
        """Whether the target supplied a register value."""
        return self.value is not None


@dataclass(frozen=True)
class SystemRegisterSet:
    """One architecture-specific system-register block read through a common API."""

    architecture: Architecture
    block_name: str
    registers: tuple[RegisterValue, ...]

    def get(self, name: str) -> RegisterValue | None:
        """Find a register by case-insensitive name."""
        normalized_name = name.upper()
        for register in self.registers:
            if register.name.upper() == normalized_name:
                return register
        return None


@dataclass(frozen=True)
class ProbeResult:
    """A probe result containing a target description or explicit unavailability."""

    target: TargetDescription | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        """Validate that a probe result has exactly one state."""
        if (self.target is None) == (self.unavailable_reason is None):
            raise ValueError("a probe result must contain a target or an unavailable reason")

    @classmethod
    def detected(cls, target: TargetDescription) -> ProbeResult:
        """Create a successful probe result."""
        return cls(target=target)

    @classmethod
    def unavailable(cls, reason: str) -> ProbeResult:
        """Create an unsuccessful probe result."""
        return cls(unavailable_reason=reason)

    @property
    def is_available(self) -> bool:
        """Whether an architecture probe identified a target."""
        return self.target is not None


class ArchitectureProbe(Protocol):
    """An architecture implementation that can identify and inspect a target."""

    architecture: Architecture

    def probe(self, reader: TargetMemory) -> ProbeResult:
        """Identify a target or return an explicit unsupported result."""
        ...

    def read_system_registers(
        self,
        reader: TargetMemory,
        target: TargetDescription,
    ) -> SystemRegisterSet:
        """Read the architecture-specific system-register block for a target."""
        ...
