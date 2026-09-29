# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Tests for Cortex-M PMSAv7 and PMSAv8 MPU inspection."""

from __future__ import annotations

import pytest

from pyGdbToolkit.arch.arm.cortex_m import CORTEX_M_CORES, CortexMPart, ScbRegister, decode_cpuid
from pyGdbToolkit.arch.arm.mpu import (
    MPU_BASE_ADDRESS,
    MpuArchitecture,
    MpuCommonRegister,
    MpuMemoryType,
    MpuV7Register,
    MpuV8Register,
    dump_mpu_regions,
)
from pyGdbToolkit.target_memory import TargetReadError, TargetWriteError


class FakeWritableTargetMemory:
    """Configurable MPU fixture with region-selector behavior."""

    def __init__(
        self,
        values: dict[int, int],
        region_values: dict[tuple[int, int], int] | None = None,
        read_failures: set[tuple[int | None, int]] | None = None,
        write_failures: set[tuple[int, int]] | None = None,
    ) -> None:
        """Initialize fixed MPU registers, selected-region values, and failures."""
        self.values = values
        self.region_values = region_values or {}
        self.read_failures = read_failures or set()
        self.write_failures = write_failures or set()
        self.selected_region = values.get(MPU_BASE_ADDRESS + MpuCommonRegister.RNR, 0)
        self.reads: list[int] = []
        self.writes: list[tuple[int, int]] = []

    def read_bytes(self, address: int, size: int) -> bytes:
        """Reject byte reads from this word-only fixture."""
        raise AssertionError((address, size))

    def read_uint16(self, address: int) -> int:
        """Reject halfword reads from this word-only fixture."""
        raise AssertionError((address, 2))

    def read_uint32(self, address: int) -> int:
        """Read a fixed or selected-region word."""
        self.reads.append(address)
        if (self.selected_region, address) in self.read_failures or (
            None,
            address,
        ) in self.read_failures:
            raise TargetReadError(address, 4, "access denied")
        if (self.selected_region, address) in self.region_values:
            return self.region_values[(self.selected_region, address)]
        if address not in self.values:
            raise TargetReadError(address, 4, "not mapped")
        return self.values[address]

    def write_uint32(self, address: int, value: int) -> None:
        """Record a selector update or simulate an inaccessible target write."""
        self.writes.append((address, value))
        if (address, value) in self.write_failures:
            raise TargetWriteError(address, 4, "access denied")
        if address != MPU_BASE_ADDRESS + MpuCommonRegister.RNR:
            raise AssertionError((address, value))
        self.selected_region = value


def _target(part: CortexMPart) -> object:
    """Build a known Arm Cortex-M target description."""
    return decode_cpuid(0x410F0000 | (part.value << 4))


@pytest.mark.parametrize("part", (CortexMPart.M3, CortexMPart.M4, CortexMPart.M7))
def test_catalog_marks_armv7_cores_with_pmsav7(part: CortexMPart) -> None:
    """Armv7-M and Armv7E-M cores use the PMSAv7 MPU interface."""
    assert CORTEX_M_CORES[part].mpu is not None
    assert CORTEX_M_CORES[part].mpu.architecture is MpuArchitecture.PMSA_V7


@pytest.mark.parametrize(
    "part",
    (
        CortexMPart.M23,
        CortexMPart.M33,
        CortexMPart.M35P,
    ),
)
def test_catalog_marks_armv8_cores_with_pmsav8(part: CortexMPart) -> None:
    """Armv8-M and Armv8.1-M cores use the PMSAv8 MPU interface."""
    assert CORTEX_M_CORES[part].mpu is not None
    assert CORTEX_M_CORES[part].mpu.architecture is MpuArchitecture.PMSA_V8
    assert not CORTEX_M_CORES[part].mpu.supports_pxn


@pytest.mark.parametrize("part", (CortexMPart.M52, CortexMPart.M55, CortexMPart.M85))
def test_catalog_marks_armv8_1_cores_with_pxn(part: CortexMPart) -> None:
    """Armv8.1-M cores interpret the PMSAv8 privileged execute-never bit."""
    assert CORTEX_M_CORES[part].mpu is not None
    assert CORTEX_M_CORES[part].mpu.supports_pxn


@pytest.mark.parametrize("part", (CortexMPart.M0, CortexMPart.M0_PLUS, CortexMPart.M1))
def test_baseline_cores_remain_outside_the_pmsav7_v8_scope(part: CortexMPart) -> None:
    """The requested PMSAv7/PMSAv8 scope excludes the Armv6-M catalog."""
    assert CORTEX_M_CORES[part].mpu is None


def test_unsupported_core_does_not_probe_mpu_registers() -> None:
    """A core outside the selected PMSA scope returns before target access."""
    reader = FakeWritableTargetMemory({})

    dump = dump_mpu_regions(reader, _target(CortexMPart.M0))  # type: ignore[arg-type]

    assert not dump.is_available
    assert reader.reads == []
    assert reader.writes == []


def test_zero_region_mpu_is_available_without_selector_writes() -> None:
    """MPU_TYPE.DREGION=0 reports runtime absence without changing MPU_RNR."""
    reader = FakeWritableTargetMemory(
        {
            MPU_BASE_ADDRESS + MpuCommonRegister.TYPE: 0,
            MPU_BASE_ADDRESS + MpuCommonRegister.CTRL: 0,
        }
    )

    dump = dump_mpu_regions(reader, _target(CortexMPart.M4))  # type: ignore[arg-type]

    assert dump.is_available
    assert dump.region_count == 0
    assert dump.regions == ()
    assert reader.writes == []


def test_mpu_type_failure_preserves_the_unavailable_register_result() -> None:
    """An unavailable dump retains target-access evidence collected before stopping."""
    type_address = MPU_BASE_ADDRESS + MpuCommonRegister.TYPE
    reader = FakeWritableTargetMemory({}, read_failures={(None, type_address)})

    dump = dump_mpu_regions(reader, _target(CortexMPart.M4))  # type: ignore[arg-type]

    assert not dump.is_available
    assert len(dump.registers) == 1
    assert dump.registers[0].name == "TYPE"
    assert not dump.registers[0].is_available


def test_decodes_pmsav7_region_and_restores_selected_region() -> None:
    """PMSAv7 regions expose ranges, permissions, cache attributes, and XN."""
    rbar = 0x20000000
    rasr = (3 << 24) | (1 << 28) | (1 << 17) | (1 << 16) | (4 << 1) | 1
    reader = FakeWritableTargetMemory(
        {
            MPU_BASE_ADDRESS + MpuCommonRegister.TYPE: 1 << 8,
            MPU_BASE_ADDRESS + MpuCommonRegister.CTRL: 1,
            MPU_BASE_ADDRESS + MpuCommonRegister.RNR: 5,
        },
        {
            (0, MPU_BASE_ADDRESS + MpuCommonRegister.RBAR): rbar,
            (0, MPU_BASE_ADDRESS + MpuV7Register.RASR): rasr,
        },
    )

    dump = dump_mpu_regions(reader, _target(CortexMPart.M4))  # type: ignore[arg-type]

    assert dump.restore_error is None
    assert dump.regions[0].is_available
    region = dump.regions[0].region
    assert region is not None
    assert region.enabled
    assert region.start_address == rbar
    assert region.end_address == rbar + 31
    assert region.size_bytes == 32
    assert region.access.value == "read-write"
    assert not region.executable
    assert region.attributes.memory_type is MpuMemoryType.NORMAL
    assert region.attributes.cacheable
    assert region.attributes.bufferable
    assert reader.writes == [
        (MPU_BASE_ADDRESS + MpuCommonRegister.RNR, 0),
        (MPU_BASE_ADDRESS + MpuCommonRegister.RNR, 5),
    ]


def test_decodes_pmsav8_device_attributes_from_mair() -> None:
    """PMSAv8 regions resolve their device-memory attributes through MAIR."""
    rbar = 0x40000000 | (0b10 << 3) | (1 << 1)
    rlar = 0x40000020 | (1 << 1) | 1
    reader = FakeWritableTargetMemory(
        {
            MPU_BASE_ADDRESS + MpuCommonRegister.TYPE: 1 << 8,
            MPU_BASE_ADDRESS + MpuCommonRegister.CTRL: 1,
            MPU_BASE_ADDRESS + MpuCommonRegister.RNR: 7,
            MPU_BASE_ADDRESS + MpuV8Register.MAIR0: 0x00000400,
            MPU_BASE_ADDRESS + MpuV8Register.MAIR1: 0,
        },
        {
            (0, MPU_BASE_ADDRESS + MpuCommonRegister.RBAR): rbar,
            (0, MPU_BASE_ADDRESS + MpuV8Register.RLAR): rlar,
        },
    )

    dump = dump_mpu_regions(reader, _target(CortexMPart.M33))  # type: ignore[arg-type]

    region = dump.regions[0].region
    assert region is not None
    assert region.start_address == 0x40000000
    assert region.end_address == 0x4000003F
    assert region.size_bytes == 64
    assert region.access.value == "read-write"
    assert region.executable
    assert region.attributes.memory_type is MpuMemoryType.DEVICE
    assert region.attributes.shareable
    assert not region.attributes.cacheable
    assert region.attributes.bufferable
    assert reader.writes[-1] == (MPU_BASE_ADDRESS + MpuCommonRegister.RNR, 7)


def test_decodes_pmsav8_normal_non_cacheable_mair_attributes() -> None:
    """MAIR 0x44 describes normal memory that is not cacheable."""
    reader = FakeWritableTargetMemory(
        {
            MPU_BASE_ADDRESS + MpuCommonRegister.TYPE: 1 << 8,
            MPU_BASE_ADDRESS + MpuCommonRegister.CTRL: 1,
            MPU_BASE_ADDRESS + MpuCommonRegister.RNR: 7,
            MPU_BASE_ADDRESS + MpuV8Register.MAIR0: 0x44,
            MPU_BASE_ADDRESS + MpuV8Register.MAIR1: 0,
        },
        {
            (0, MPU_BASE_ADDRESS + MpuCommonRegister.RBAR): 0x20000000 | (0b10 << 3),
            (0, MPU_BASE_ADDRESS + MpuV8Register.RLAR): 0x20000020 | 1,
        },
    )

    dump = dump_mpu_regions(reader, _target(CortexMPart.M33))  # type: ignore[arg-type]

    region = dump.regions[0].region
    assert region is not None
    assert region.attributes.memory_type is MpuMemoryType.NORMAL
    assert region.attributes.shareable
    assert not region.attributes.cacheable


@pytest.mark.parametrize(
    ("rbar_flags", "rlar_flags", "privileged", "unprivileged"),
    (
        (0, 0, True, True),
        (0, 1 << 4, False, True),
        (1, 0, False, False),
        (1, 1 << 4, False, False),
    ),
)
def test_pmsav8_1_xn_takes_precedence_over_pxn(
    rbar_flags: int,
    rlar_flags: int,
    privileged: bool,
    unprivileged: bool,
) -> None:
    """Armv8.1-M PXN restricts only privileged execution when XN is clear."""
    reader = FakeWritableTargetMemory(
        {
            MPU_BASE_ADDRESS + MpuCommonRegister.TYPE: 1 << 8,
            MPU_BASE_ADDRESS + MpuCommonRegister.CTRL: 1,
            MPU_BASE_ADDRESS + MpuCommonRegister.RNR: 7,
            MPU_BASE_ADDRESS + MpuV8Register.MAIR0: 0,
            MPU_BASE_ADDRESS + MpuV8Register.MAIR1: 0,
        },
        {
            (0, MPU_BASE_ADDRESS + MpuCommonRegister.RBAR): 0x20000000 | rbar_flags,
            (0, MPU_BASE_ADDRESS + MpuV8Register.RLAR): 0x20000020 | rlar_flags | 1,
        },
    )

    dump = dump_mpu_regions(reader, _target(CortexMPart.M55))  # type: ignore[arg-type]

    region = dump.regions[0].region
    assert region is not None
    assert region.privileged_executable is privileged
    assert region.unprivileged_executable is unprivileged
    assert region.executable is (privileged or unprivileged)


def test_pmsav8_ignores_pxn_bit_outside_armv8_1_m() -> None:
    """A PXN bit in an earlier PMSAv8 target does not alter decoded permissions."""
    reader = FakeWritableTargetMemory(
        {
            MPU_BASE_ADDRESS + MpuCommonRegister.TYPE: 1 << 8,
            MPU_BASE_ADDRESS + MpuCommonRegister.CTRL: 1,
            MPU_BASE_ADDRESS + MpuCommonRegister.RNR: 7,
            MPU_BASE_ADDRESS + MpuV8Register.MAIR0: 0,
            MPU_BASE_ADDRESS + MpuV8Register.MAIR1: 0,
        },
        {
            (0, MPU_BASE_ADDRESS + MpuCommonRegister.RBAR): 0x20000000,
            (0, MPU_BASE_ADDRESS + MpuV8Register.RLAR): 0x20000020 | (1 << 4) | 1,
        },
    )

    dump = dump_mpu_regions(reader, _target(CortexMPart.M33))  # type: ignore[arg-type]

    region = dump.regions[0].region
    assert region is not None
    assert region.privileged_executable
    assert region.unprivileged_executable


def test_region_read_failure_is_retained_and_selector_is_restored() -> None:
    """A failed region read does not prevent restoring the prior MPU_RNR value."""
    rnr_address = MPU_BASE_ADDRESS + MpuCommonRegister.RNR
    reader = FakeWritableTargetMemory(
        {
            MPU_BASE_ADDRESS + MpuCommonRegister.TYPE: 1 << 8,
            MPU_BASE_ADDRESS + MpuCommonRegister.CTRL: 0,
            rnr_address: 3,
        },
        read_failures={(0, MPU_BASE_ADDRESS + MpuCommonRegister.RBAR)},
    )

    dump = dump_mpu_regions(reader, _target(CortexMPart.M7))  # type: ignore[arg-type]

    assert not dump.regions[0].is_available
    assert "access denied" in dump.regions[0].unavailable_reason
    assert dump.restore_error is None
    assert reader.writes[-1] == (rnr_address, 3)


def test_restore_failure_is_exposed_in_the_dump() -> None:
    """An MPU_RNR restoration failure is retained instead of being suppressed."""
    rnr_address = MPU_BASE_ADDRESS + MpuCommonRegister.RNR
    reader = FakeWritableTargetMemory(
        {
            MPU_BASE_ADDRESS + MpuCommonRegister.TYPE: 1 << 8,
            MPU_BASE_ADDRESS + MpuCommonRegister.CTRL: 0,
            rnr_address: 3,
        },
        {
            (0, MPU_BASE_ADDRESS + MpuCommonRegister.RBAR): 0x20000000,
            (0, MPU_BASE_ADDRESS + MpuV7Register.RASR): (4 << 1) | 1,
        },
        write_failures={(rnr_address, 3)},
    )

    dump = dump_mpu_regions(reader, _target(CortexMPart.M3))  # type: ignore[arg-type]

    assert dump.restore_error is not None
    assert "could not write 4 byte(s) at 0xE000ED98: access denied" == dump.restore_error


def test_mpu_registers_are_not_added_to_scb_registers() -> None:
    """MPU register descriptions remain separate from the SCB register map."""
    mpu_register_names = {
        MpuCommonRegister.TYPE.name,
        MpuCommonRegister.CTRL.name,
        MpuCommonRegister.RNR.name,
        MpuCommonRegister.RBAR.name,
        MpuV7Register.RASR.name,
        MpuV8Register.RLAR.name,
        MpuV8Register.MAIR0.name,
        MpuV8Register.MAIR1.name,
    }

    assert mpu_register_names.isdisjoint(register.name for register in ScbRegister)
