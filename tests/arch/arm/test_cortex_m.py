# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Tests for Cortex-M target descriptions and SCB register inspection."""

from __future__ import annotations

import pytest

from pyGdbToolkit.arch import DEFAULT_ARCHITECTURE_REGISTRY
from pyGdbToolkit.arch.arm.cortex_m import (
    CPUID_ADDRESS,
    SCB_BASE_ADDRESS,
    CORTEX_M_CORES,
    CortexMArchitecture,
    CortexMFeature,
    CortexMPart,
    ScbRegister,
    decode_cpuid,
    inspect_cortex_m,
    read_scb,
)
from pyGdbToolkit.target_memory import TargetReadError


class FakeTargetMemory:
    """Configurable 32-bit target-memory reader recording requested addresses."""

    def __init__(self, values: dict[int, int], inaccessible: set[int] | None = None) -> None:
        """Initialize readable words and optional inaccessible addresses."""
        self.values = values
        self.inaccessible = inaccessible or set()
        self.calls: list[int] = []

    def read_bytes(self, address: int, size: int) -> bytes:
        """Reject byte reads from this word-only fixture."""
        raise AssertionError((address, size))

    def read_uint16(self, address: int) -> int:
        """Reject halfword reads from this word-only fixture."""
        raise AssertionError((address, 2))

    def read_uint32(self, address: int) -> int:
        """Read a configured word or report a target access failure."""
        self.calls.append(address)
        if address in self.inaccessible:
            raise TargetReadError(address, 4, "access denied")
        if address not in self.values:
            raise TargetReadError(address, 4, "not mapped")
        return self.values[address]


def _cpuid(part: CortexMPart, *, variant: int = 0, patch: int = 0) -> int:
    """Build an Arm CPUID value for a known Cortex-M part."""
    return 0x410F0000 | (variant << 20) | (part.value << 4) | patch


@pytest.mark.parametrize("part", tuple(CORTEX_M_CORES))
def test_catalog_decodes_every_cortex_m_part(part: CortexMPart) -> None:
    """Every known Cortex-M part remains represented in the catalog."""
    target = decode_cpuid(_cpuid(part, variant=2, patch=3))

    assert target.core is not None
    assert target.core.part is part
    assert target.core_name == target.core.core_name
    assert target.rnp_revision == "r2p3"


@pytest.mark.parametrize("part", (CortexMPart.M4, CortexMPart.M7))
def test_cortex_m4_and_m7_are_armv7e_m_with_dsp(part: CortexMPart) -> None:
    """Cortex-M4 and Cortex-M7 implement the Armv7E-M DSP extension."""
    target = decode_cpuid(_cpuid(part))

    assert target.core is not None
    assert target.core.architecture_version is CortexMArchitecture.ARMV7E_M
    assert CortexMFeature.DSP_EXTENSION in target.core.features


def test_unknown_part_returns_cpuid_only_without_scb_reads() -> None:
    """An unknown part preserves CPUID data and never probes the SCB."""
    raw_cpuid = 0x410F1234
    reader = FakeTargetMemory({CPUID_ADDRESS: raw_cpuid})

    target, scb = inspect_cortex_m(reader)

    assert target.core is None
    assert target.core_name == "Unknown Cortex-M part 0x123"
    assert scb.registers == ()
    assert reader.calls == [CPUID_ADDRESS]


def test_known_part_reads_its_scb_once_and_keeps_per_register_failures() -> None:
    """Known cores use their map and retain a failed SCB register as unavailable."""
    raw_cpuid = _cpuid(CortexMPart.M4)
    core = CORTEX_M_CORES[CortexMPart.M4]
    inaccessible_address = SCB_BASE_ADDRESS + ScbRegister.BFAR
    values = {SCB_BASE_ADDRESS + register.value: register.value for register in core.scb_registers}
    values[CPUID_ADDRESS] = raw_cpuid
    reader = FakeTargetMemory(values, {inaccessible_address})

    target, scb = inspect_cortex_m(reader)

    assert target.core is core
    assert reader.calls.count(CPUID_ADDRESS) == 1
    assert tuple(value.name for value in scb.registers) == tuple(
        register.name for register in core.scb_registers
    )
    bfar = scb.get("BFAR")
    assert bfar is not None
    assert not bfar.is_available
    assert bfar.unavailable_reason == ("could not read 4 byte(s) at 0xE000ED38: access denied")


def test_cortex_m23_keeps_secure_fault_registers_without_mainline_fault_registers() -> None:
    """The baseline Cortex-M23 maps Security Extension faults, not mainline fault registers."""
    target = decode_cpuid(_cpuid(CortexMPart.M23))
    assert target.core is not None
    values = {
        SCB_BASE_ADDRESS + register.value: register.value for register in target.core.scb_registers
    }
    values[CPUID_ADDRESS] = target.raw_cpuid
    reader = FakeTargetMemory(values)

    scb = read_scb(reader, target, cpuid_value=target.raw_cpuid)

    assert scb.get("CFSR") is None
    sfsr = scb.get("SFSR")
    sfar = scb.get("SFAR")
    assert sfsr is not None and sfsr.address == 0xE000EDE4
    assert sfar is not None and sfar.address == 0xE000EDE8
    assert SCB_BASE_ADDRESS + 0x308 not in reader.calls
    assert SCB_BASE_ADDRESS + ScbRegister.CFSR not in reader.calls


def test_cortex_m33_uses_cmsis_secure_fault_addresses() -> None:
    """Secure-fault registers use the CMSIS SCB offsets rather than legacy aliases."""
    target = decode_cpuid(_cpuid(CortexMPart.M33))
    assert target.core is not None
    values = {
        SCB_BASE_ADDRESS + register.value: register.value for register in target.core.scb_registers
    }
    reader = FakeTargetMemory(values)

    scb = read_scb(reader, target)

    sfsr = scb.get("SFSR")
    sfar = scb.get("SFAR")
    assert sfsr is not None and sfsr.address == 0xE000EDE4
    assert sfar is not None and sfar.address == 0xE000EDE8


def test_cortex_m85_uses_its_dedicated_cmsis_scb_map() -> None:
    """Cortex-M85 includes RFSR and does not inherit the Cortex-M7 AHBSCR."""
    target = decode_cpuid(_cpuid(CortexMPart.M85))
    assert target.core is not None
    values = {
        SCB_BASE_ADDRESS + register.value: register.value for register in target.core.scb_registers
    }
    reader = FakeTargetMemory(values)

    scb = read_scb(reader, target)

    rfsr = scb.get("RFSR")
    assert rfsr is not None and rfsr.address == 0xE000EF04
    assert scb.get("AHBSCR") is None
    assert scb.get("ID_ISAR5") is not None
    assert scb.get("CSSELR") is not None
    assert sum(value.name == "CPACR" for value in scb.registers) == 1


def test_default_registry_recognizes_known_cortex_m_target() -> None:
    """The default portable registry detects registered Cortex-M CPUID parts."""
    reader = FakeTargetMemory({CPUID_ADDRESS: _cpuid(CortexMPart.M7)})

    result = DEFAULT_ARCHITECTURE_REGISTRY.probe(reader)

    assert result.is_available
    assert result.target is not None
    assert result.target.core_name == "Cortex-M7"


def test_default_registry_rejects_unknown_cortex_m_part() -> None:
    """The generic registry does not claim ownership of an unknown Cortex-M part."""
    reader = FakeTargetMemory({CPUID_ADDRESS: 0x410F1234})

    result = DEFAULT_ARCHITECTURE_REGISTRY.probe(reader)

    assert not result.is_available


def test_default_registry_rejects_invalid_cortex_m_cpuid_signature() -> None:
    """Part-number bits alone cannot make an arbitrary word a Cortex-M CPUID."""
    reader = FakeTargetMemory({CPUID_ADDRESS: 0x000FC240})

    result = DEFAULT_ARCHITECTURE_REGISTRY.probe(reader)

    assert not result.is_available
    assert result.unavailable_reason == "no registered architecture probe recognized the target"


def test_default_registry_continues_after_an_inaccessible_cortex_m_cpuid() -> None:
    """CPUID access errors leave the registry available for later architecture probes."""
    reader = FakeTargetMemory({}, {CPUID_ADDRESS})

    result = DEFAULT_ARCHITECTURE_REGISTRY.probe(reader)

    assert not result.is_available
