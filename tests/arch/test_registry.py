# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Tests for portable architecture probe registry behavior."""

from __future__ import annotations

from dataclasses import dataclass

from pyGdbToolkit.arch import Architecture, ArchitectureRegistry, ProbeResult, TargetDescription
from pyGdbToolkit.arch.base import SystemRegisterSet
from pyGdbToolkit.target_memory import TargetMemory


@dataclass
class FakeProbe:
    """Probe fixture that returns its configured result and records invocation."""

    architecture: Architecture
    result: ProbeResult
    was_called: bool = False

    def probe(self, reader: TargetMemory) -> ProbeResult:
        """Return the configured probe result."""
        del reader
        self.was_called = True
        return self.result

    def read_system_registers(
        self,
        reader: TargetMemory,
        target: TargetDescription,
    ) -> SystemRegisterSet:
        """Provide the protocol method required by the registry fixture."""
        del reader
        return SystemRegisterSet(target.architecture, "test", ())


class FakeReader:
    """Reader placeholder unused by these registry-only tests."""

    def read_bytes(self, address: int, size: int) -> bytes:
        """Reject unused byte reads."""
        raise AssertionError((address, size))

    def read_uint16(self, address: int) -> int:
        """Reject unused halfword reads."""
        raise AssertionError((address, 2))

    def read_uint32(self, address: int) -> int:
        """Reject unused word reads."""
        raise AssertionError((address, 4))


def test_registry_stops_after_first_architecture_match() -> None:
    """A recognized target prevents lower-priority probes from running."""
    target = TargetDescription(Architecture.ARM, "Arm", "Cortex-M4", "r0p1")
    first = FakeProbe(Architecture.ARM, ProbeResult.detected(target))
    second = FakeProbe(Architecture.RISCV, ProbeResult.unavailable("not RISC-V"))

    result = ArchitectureRegistry((first, second)).probe(FakeReader())

    assert result.target == target
    assert first.was_called
    assert not second.was_called


def test_registry_returns_explicit_unsupported_result() -> None:
    """An unrecognized target is not misclassified as the first registered architecture."""
    probe = FakeProbe(Architecture.ARM, ProbeResult.unavailable("not an Arm target"))

    result = ArchitectureRegistry((probe,)).probe(FakeReader())

    assert not result.is_available
    assert result.unavailable_reason == "no registered architecture probe recognized the target"
