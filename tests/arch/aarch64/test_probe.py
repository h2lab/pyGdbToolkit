# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Tests for minimal AArch64 recognition and session loading."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pyGdbToolkit.arch import (
    DEFAULT_ARCHITECTURE_REGISTRY,
    DEFAULT_DIAGNOSTIC_RUNTIME,
    Architecture,
    DiagnosticServiceName,
    TargetDescription,
)
from pyGdbToolkit.arch.aarch64 import AArch64Probe, AArch64TargetDescription
from pyGdbToolkit.arch.arm.cortex_m import CPUID_ADDRESS, CortexMTargetDescription
from pyGdbToolkit.session import ToolkitSession
from pyGdbToolkit.target_memory import TargetMemoryReader


class MetadataReader:
    """Expose architecture metadata and reject any target-memory access."""

    def __init__(self, architecture_name: str | None) -> None:
        """Set the architecture reported by GDB."""
        self.architecture_name = architecture_name

    def read_bytes(self, address: int, size: int) -> bytes:
        """Reject unexpected byte reads."""
        raise AssertionError((address, size))

    def read_uint16(self, address: int) -> int:
        """Reject unexpected halfword reads."""
        raise AssertionError((address, 2))

    def read_uint32(self, address: int) -> int:
        """Reject unexpected word reads, including Cortex-M CPUID."""
        raise AssertionError((address, 4))


@pytest.mark.parametrize("name", ("aarch64", "aarch64:ilp32"))
def test_registry_recognizes_aarch64_without_memory_reads(name: str) -> None:
    """AArch64 metadata short-circuits the Cortex-M memory probe."""
    result = DEFAULT_ARCHITECTURE_REGISTRY.probe(MetadataReader(name))

    assert isinstance(result.target, AArch64TargetDescription)
    assert result.target.architecture is Architecture.AARCH64
    assert result.target.gdb_architecture == name
    assert result.target.core_name == "AArch64"
    assert result.target.revision == "unknown"


@pytest.mark.parametrize("name", (None, "arm", "armv8-a", "armv8-m.main", "i386"))
def test_probe_does_not_guess_aarch64(name: str | None) -> None:
    """Neither missing metadata nor ambiguous Arm names imply AArch64."""
    assert not AArch64Probe().probe(MetadataReader(name)).is_available


def test_aarch64_system_register_support_is_initially_empty() -> None:
    """The initial backend does not invent system registers or access memory."""
    reader = MetadataReader("aarch64")
    target = AArch64Probe().probe(reader).target
    assert target is not None

    registers = AArch64Probe().read_system_registers(reader, target)

    assert registers.architecture is Architecture.AARCH64
    assert registers.registers == ()


def test_aarch64_probe_rejects_arm_register_description() -> None:
    """The AArch64 backend remains separate from Arm target descriptions."""
    target = TargetDescription(Architecture.ARM, "Arm", "Cortex-M7", "r1p2")
    with pytest.raises(ValueError, match="AArch64TargetDescription"):
        AArch64Probe().read_system_registers(MetadataReader("arm"), target)


def test_session_loads_aarch64_from_gdb_inferior(fake_gdb: SimpleNamespace) -> None:
    """The default session recognizes AArch64 using the real memory-reader adapter."""
    fake_gdb._inferior = SimpleNamespace(
        architecture=lambda: SimpleNamespace(name=lambda: "aarch64")
    )
    session = ToolkitSession()

    assert session.architecture is Architecture.AARCH64
    assert isinstance(session.require_target(), AArch64TargetDescription)
    assert session.require_target() is session.require_target()


def test_memory_architecture_uses_bound_inferior(fake_gdb: SimpleNamespace) -> None:
    """Changing the selected inferior does not change an existing reader's identity."""
    fake_gdb._inferior = SimpleNamespace(
        architecture=lambda: SimpleNamespace(name=lambda: "aarch64")
    )
    reader = TargetMemoryReader()
    fake_gdb._inferior = SimpleNamespace(architecture=lambda: SimpleNamespace(name=lambda: "arm"))

    assert reader.architecture_name == "aarch64"


def test_session_invalidation_restores_arm_detection(fake_gdb: SimpleNamespace) -> None:
    """A session can move from AArch64 to Cortex-M without reusing its cached identity."""
    fake_gdb._inferior = SimpleNamespace(
        architecture=lambda: SimpleNamespace(name=lambda: "aarch64")
    )
    session = ToolkitSession()
    assert session.architecture is Architecture.AARCH64
    reads: list[tuple[int, int]] = []

    def read_arm_memory(address: int, size: int) -> bytes:
        reads.append((address, size))
        assert (address, size) == (CPUID_ADDRESS, 4)
        return (0x411FC272).to_bytes(4, byteorder="little")

    fake_gdb._inferior = SimpleNamespace(
        architecture=lambda: SimpleNamespace(name=lambda: "arm"),
        read_memory=read_arm_memory,
    )
    session.invalidate()

    assert session.architecture is Architecture.ARM
    assert isinstance(session.require_target(), CortexMTargetDescription)
    assert session.require_target().core_name == "Cortex-M7"
    assert reads == [(CPUID_ADDRESS, 4)]


def test_unavailable_gdb_architecture_is_explicit(fake_gdb: SimpleNamespace) -> None:
    """An unavailable GDB architecture does not become a guessed CPU identity."""

    def unavailable_architecture() -> None:
        raise fake_gdb.error("architecture unavailable")

    fake_gdb._inferior = SimpleNamespace(architecture=unavailable_architecture)

    assert TargetMemoryReader().architecture_name is None


@pytest.mark.parametrize("service", tuple(DiagnosticServiceName))
def test_cortex_m_diagnostics_are_not_dispatched_on_aarch64(
    service: DiagnosticServiceName,
) -> None:
    """Unsupported services return unavailability without Cortex-M memory reads."""
    result = DEFAULT_DIAGNOSTIC_RUNTIME.diagnose(MetadataReader("aarch64"), service)

    assert result.target is not None
    assert result.target.architecture is Architecture.AARCH64
    assert result.unavailable_reason is not None
    assert "not registered" in result.unavailable_reason
