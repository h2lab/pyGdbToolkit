# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Cortex-M PMSAv7 and PMSAv8 Memory Protection Unit inspection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING

from ...target_memory import TargetReadError, TargetWriteError, WritableTargetMemory
from ..base import RegisterValue

if TYPE_CHECKING:
    from .cortex_m import CortexMTargetDescription


MPU_BASE_ADDRESS = 0xE000ED90


class MpuArchitecture(StrEnum):
    """Protected Memory System Architecture versions supported by this module."""

    PMSA_V7 = "PMSAv7"
    PMSA_V8 = "PMSAv8"


class MpuCommonRegister(IntEnum):
    """MPU register offsets shared by PMSAv7 and PMSAv8."""

    TYPE = 0x000
    CTRL = 0x004
    RNR = 0x008
    RBAR = 0x00C


class MpuV7Register(IntEnum):
    """PMSAv7-specific MPU register offsets."""

    RASR = 0x010


class MpuV8Register(IntEnum):
    """PMSAv8-specific MPU register offsets."""

    RLAR = 0x010
    RBAR_A1 = 0x014
    RLAR_A1 = 0x018
    RBAR_A2 = 0x01C
    RLAR_A2 = 0x020
    RBAR_A3 = 0x024
    RLAR_A3 = 0x028
    MAIR0 = 0x030
    MAIR1 = 0x034


class MpuAccessPermission(StrEnum):
    """Normalized MPU access permissions."""

    NO_ACCESS = "no-access"
    PRIVILEGED_READ_WRITE = "privileged-read-write"
    PRIVILEGED_READ_WRITE_USER_READ = "privileged-read-write-user-read"
    READ_WRITE = "read-write"
    PRIVILEGED_READ_ONLY = "privileged-read-only"
    READ_ONLY = "read-only"
    RESERVED = "reserved"


class MpuMemoryType(StrEnum):
    """Memory types represented by PMSA MPU attributes."""

    STRONGLY_ORDERED = "strongly-ordered"
    DEVICE = "device"
    NORMAL = "normal"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MpuDescription:
    """Static MPU interface description for one Cortex-M core."""

    architecture: MpuArchitecture
    registers: tuple[MpuCommonRegister | MpuV7Register | MpuV8Register, ...]
    supports_pxn: bool


@dataclass(frozen=True)
class MpuMemoryAttributes:
    """Decoded memory-type and cacheability attributes for one MPU region."""

    memory_type: MpuMemoryType
    shareable: bool | None
    cacheable: bool | None
    bufferable: bool | None
    attribute_index: int | None
    raw: int
    description: str


@dataclass(frozen=True)
class MpuRegion:
    """One decoded MPU region configuration."""

    index: int
    enabled: bool
    start_address: int
    end_address: int | None
    size_bytes: int | None
    access: MpuAccessPermission
    privileged_executable: bool
    unprivileged_executable: bool
    attributes: MpuMemoryAttributes
    subregion_disable_mask: int | None
    raw_rbar: int
    raw_attributes: int

    @property
    def executable(self) -> bool:
        """Whether either privileged or unprivileged code can execute the region."""
        return self.privileged_executable or self.unprivileged_executable


@dataclass(frozen=True)
class MpuRegionResult:
    """A decoded MPU region or its explicit selection/read failure."""

    index: int
    region: MpuRegion | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        """Require exactly one region result state."""
        if (self.region is None) == (self.unavailable_reason is None):
            raise ValueError("an MPU region result must be available or unavailable")

    @property
    def is_available(self) -> bool:
        """Whether the selected region was read and decoded."""
        return self.region is not None

    @classmethod
    def unavailable(cls, index: int, reason: str) -> MpuRegionResult:
        """Create an unavailable region result."""
        return cls(index=index, unavailable_reason=reason)


@dataclass(frozen=True)
class MpuDump:
    """MPU configuration and decoded region results for one Cortex-M target."""

    description: MpuDescription | None
    registers: tuple[RegisterValue, ...]
    region_count: int | None
    regions: tuple[MpuRegionResult, ...]
    unavailable_reason: str | None = None
    restore_error: str | None = None

    @property
    def is_available(self) -> bool:
        """Whether the target's MPU configuration was inspected."""
        return self.unavailable_reason is None

    @classmethod
    def unavailable(
        cls,
        description: MpuDescription | None,
        reason: str,
        registers: tuple[RegisterValue, ...] = (),
    ) -> MpuDump:
        """Create an explicit unavailable MPU inspection result."""
        return cls(
            description=description,
            registers=registers,
            region_count=None,
            regions=(),
            unavailable_reason=reason,
        )

    def get_register(self, name: str) -> RegisterValue | None:
        """Find a captured MPU register by case-insensitive name."""
        normalized_name = name.upper()
        for register in self.registers:
            if register.name.upper() == normalized_name:
                return register
        return None


PMSA_V7_MPU = MpuDescription(
    MpuArchitecture.PMSA_V7,
    (
        MpuCommonRegister.TYPE,
        MpuCommonRegister.CTRL,
        MpuCommonRegister.RNR,
        MpuCommonRegister.RBAR,
        MpuV7Register.RASR,
    ),
    False,
)
PMSA_V8_MPU = MpuDescription(
    MpuArchitecture.PMSA_V8,
    (
        MpuCommonRegister.TYPE,
        MpuCommonRegister.CTRL,
        MpuCommonRegister.RNR,
        MpuCommonRegister.RBAR,
        MpuV8Register.RLAR,
        MpuV8Register.RBAR_A1,
        MpuV8Register.RLAR_A1,
        MpuV8Register.RBAR_A2,
        MpuV8Register.RLAR_A2,
        MpuV8Register.RBAR_A3,
        MpuV8Register.RLAR_A3,
        MpuV8Register.MAIR0,
        MpuV8Register.MAIR1,
    ),
    False,
)
PMSA_V8_PXN_MPU = MpuDescription(
    MpuArchitecture.PMSA_V8,
    (
        MpuCommonRegister.TYPE,
        MpuCommonRegister.CTRL,
        MpuCommonRegister.RNR,
        MpuCommonRegister.RBAR,
        MpuV8Register.RLAR,
        MpuV8Register.RBAR_A1,
        MpuV8Register.RLAR_A1,
        MpuV8Register.RBAR_A2,
        MpuV8Register.RLAR_A2,
        MpuV8Register.RBAR_A3,
        MpuV8Register.RLAR_A3,
        MpuV8Register.MAIR0,
        MpuV8Register.MAIR1,
    ),
    True,
)


def dump_mpu_regions(reader: WritableTargetMemory, target: CortexMTargetDescription) -> MpuDump:
    """Read and decode MPU region configuration for a known Cortex-M core.

    The function writes each region index to ``MPU_RNR`` and restores the
    target's original selector value before returning. Region read failures are
    retained per region; a selector restoration failure is retained in
    :attr:`MpuDump.restore_error`.

    Parameters
    ----------
    reader
        Target-memory reader and writer used for MPU registers.
    target
        Known Cortex-M target description returned by :func:`decode_cpuid`.

    Returns
    -------
    MpuDump
        MPU register values, decoded regions, or an explicit unavailable reason.
    """
    description = target.core.mpu if target.core is not None else None
    if description is None:
        return MpuDump.unavailable(
            None,
            "this Cortex-M core has no PMSAv7 or PMSAv8 MPU description",
        )

    registers: list[RegisterValue] = []
    mpu_type = _read_register(reader, MpuCommonRegister.TYPE)
    registers.append(mpu_type)
    if not mpu_type.is_available:
        assert mpu_type.unavailable_reason is not None
        return MpuDump.unavailable(description, mpu_type.unavailable_reason, tuple(registers))

    assert mpu_type.value is not None
    region_count = (mpu_type.value >> 8) & 0xFF
    mpu_control = _read_register(reader, MpuCommonRegister.CTRL)
    registers.append(mpu_control)
    if region_count == 0:
        return MpuDump(description, tuple(registers), 0, ())

    mair_values: tuple[RegisterValue, RegisterValue] | None = None
    if description.architecture is MpuArchitecture.PMSA_V8:
        mair0 = _read_register(reader, MpuV8Register.MAIR0)
        mair1 = _read_register(reader, MpuV8Register.MAIR1)
        registers.extend((mair0, mair1))
        mair_values = (mair0, mair1)

    original_rnr = _read_register(reader, MpuCommonRegister.RNR)
    registers.append(original_rnr)
    if not original_rnr.is_available:
        assert original_rnr.unavailable_reason is not None
        return MpuDump.unavailable(description, original_rnr.unavailable_reason, tuple(registers))

    assert original_rnr.value is not None
    regions: list[MpuRegionResult] = []
    restore_error: str | None = None
    try:
        for index in range(region_count):
            try:
                reader.write_uint32(MPU_BASE_ADDRESS + MpuCommonRegister.RNR, index)
            except TargetWriteError as error:
                regions.append(MpuRegionResult.unavailable(index, str(error)))
                continue

            regions.append(_read_region(reader, description, index, mair_values))
    finally:
        try:
            reader.write_uint32(MPU_BASE_ADDRESS + MpuCommonRegister.RNR, original_rnr.value)
        except TargetWriteError as error:
            restore_error = str(error)

    return MpuDump(
        description,
        tuple(registers),
        region_count,
        tuple(regions),
        restore_error=restore_error,
    )


def _read_register(
    reader: WritableTargetMemory,
    register: MpuCommonRegister | MpuV7Register | MpuV8Register,
) -> RegisterValue:
    """Read one MPU register while preserving a target access failure."""
    address = MPU_BASE_ADDRESS + register.value
    try:
        value = reader.read_uint32(address)
    except TargetReadError as error:
        return RegisterValue.unavailable(register.name, address, 32, str(error))
    return RegisterValue.known(register.name, address, 32, value)


def _read_region(
    reader: WritableTargetMemory,
    description: MpuDescription,
    index: int,
    mair_values: tuple[RegisterValue, RegisterValue] | None,
) -> MpuRegionResult:
    """Read and decode the currently selected MPU region."""
    try:
        rbar = reader.read_uint32(MPU_BASE_ADDRESS + MpuCommonRegister.RBAR)
        attributes_offset = (
            MpuV7Register.RASR
            if description.architecture is MpuArchitecture.PMSA_V7
            else MpuV8Register.RLAR
        )
        raw_attributes = reader.read_uint32(MPU_BASE_ADDRESS + attributes_offset)
    except TargetReadError as error:
        return MpuRegionResult.unavailable(index, str(error))

    if description.architecture is MpuArchitecture.PMSA_V7:
        return MpuRegionResult(
            index=index,
            region=_decode_pmsa_v7_region(index, rbar, raw_attributes),
        )
    assert mair_values is not None
    return MpuRegionResult(
        index=index,
        region=_decode_pmsa_v8_region(description, index, rbar, raw_attributes, mair_values),
    )


def _decode_pmsa_v7_region(index: int, rbar: int, rasr: int) -> MpuRegion:
    """Decode a PMSAv7 ``RBAR``/``RASR`` region pair."""
    size_encoding = (rasr >> 1) & 0x1F
    size_bytes = 1 << (size_encoding + 1) if size_encoding >= 4 else None
    start_address = rbar & 0xFFFFFFE0
    end_address = start_address + size_bytes - 1 if size_bytes is not None else None
    ap = (rasr >> 24) & 0x7
    tex = (rasr >> 19) & 0x7
    shareable = bool(rasr & (1 << 18))
    cacheable = bool(rasr & (1 << 17))
    bufferable = bool(rasr & (1 << 16))
    execute_never = bool(rasr & (1 << 28))
    return MpuRegion(
        index=index,
        enabled=bool(rasr & 0x1),
        start_address=start_address,
        end_address=end_address,
        size_bytes=size_bytes,
        access=_decode_pmsa_v7_access(ap),
        privileged_executable=not execute_never,
        unprivileged_executable=not execute_never,
        attributes=_decode_pmsa_v7_attributes(tex, shareable, cacheable, bufferable, rasr),
        subregion_disable_mask=(rasr >> 8) & 0xFF,
        raw_rbar=rbar,
        raw_attributes=rasr,
    )


def _decode_pmsa_v8_region(
    description: MpuDescription,
    index: int,
    rbar: int,
    rlar: int,
    mair_values: tuple[RegisterValue, RegisterValue],
) -> MpuRegion:
    """Decode a PMSAv8 ``RBAR``/``RLAR`` region pair."""
    attribute_index = (rlar >> 1) & 0x7
    start_address = rbar & 0xFFFFFFE0
    end_address = (rlar & 0xFFFFFFE0) | 0x1F
    size_bytes = end_address - start_address + 1 if end_address >= start_address else None
    execute_never = bool(rbar & 0x1)
    privileged_execute_never = description.supports_pxn and bool(rlar & (1 << 4))
    return MpuRegion(
        index=index,
        enabled=bool(rlar & 0x1),
        start_address=start_address,
        end_address=end_address,
        size_bytes=size_bytes,
        access=_decode_pmsa_v8_access((rbar >> 1) & 0x3),
        privileged_executable=not (execute_never or privileged_execute_never),
        unprivileged_executable=not execute_never,
        attributes=_decode_pmsa_v8_attributes(
            attribute_index,
            ((rbar >> 3) & 0x3) in (0b10, 0b11),
            mair_values,
        ),
        subregion_disable_mask=None,
        raw_rbar=rbar,
        raw_attributes=rlar,
    )


def _decode_pmsa_v7_access(ap: int) -> MpuAccessPermission:
    """Decode PMSAv7 three-bit access permission fields."""
    permissions = {
        0b000: MpuAccessPermission.NO_ACCESS,
        0b001: MpuAccessPermission.PRIVILEGED_READ_WRITE,
        0b010: MpuAccessPermission.PRIVILEGED_READ_WRITE_USER_READ,
        0b011: MpuAccessPermission.READ_WRITE,
        0b101: MpuAccessPermission.PRIVILEGED_READ_ONLY,
        0b110: MpuAccessPermission.READ_ONLY,
    }
    return permissions.get(ap, MpuAccessPermission.RESERVED)


def _decode_pmsa_v8_access(ap: int) -> MpuAccessPermission:
    """Decode PMSAv8 two-bit access permission fields."""
    permissions = {
        0b00: MpuAccessPermission.PRIVILEGED_READ_WRITE,
        0b01: MpuAccessPermission.READ_WRITE,
        0b10: MpuAccessPermission.PRIVILEGED_READ_ONLY,
        0b11: MpuAccessPermission.READ_ONLY,
    }
    return permissions[ap]


def _decode_pmsa_v7_attributes(
    tex: int,
    shareable: bool,
    cacheable: bool,
    bufferable: bool,
    rasr: int,
) -> MpuMemoryAttributes:
    """Decode the PMSAv7 TEX/S/C/B memory attribute fields."""
    if tex == 0 and not cacheable and not bufferable:
        return MpuMemoryAttributes(
            MpuMemoryType.STRONGLY_ORDERED,
            True,
            False,
            False,
            None,
            rasr,
            "strongly ordered",
        )
    if (tex == 0 and not cacheable and bufferable) or (tex == 2 and not cacheable):
        return MpuMemoryAttributes(
            MpuMemoryType.DEVICE,
            shareable,
            False,
            bufferable,
            None,
            rasr,
            "device memory",
        )
    if tex in (0, 1) or tex >= 4:
        return MpuMemoryAttributes(
            MpuMemoryType.NORMAL,
            shareable,
            cacheable,
            bufferable,
            None,
            rasr,
            "normal memory",
        )
    return MpuMemoryAttributes(
        MpuMemoryType.UNKNOWN,
        shareable,
        cacheable,
        bufferable,
        None,
        rasr,
        "reserved PMSAv7 TEX/S/C/B encoding",
    )


def _decode_pmsa_v8_attributes(
    attribute_index: int,
    shareable: bool,
    mair_values: tuple[RegisterValue, RegisterValue],
) -> MpuMemoryAttributes:
    """Resolve a PMSAv8 region attribute index through MAIR."""
    mair_register = mair_values[attribute_index // 4]
    if not mair_register.is_available:
        assert mair_register.unavailable_reason is not None
        return MpuMemoryAttributes(
            MpuMemoryType.UNKNOWN,
            shareable,
            None,
            None,
            attribute_index,
            0,
            f"{mair_register.name} unavailable: {mair_register.unavailable_reason}",
        )

    assert mair_register.value is not None
    attribute = (mair_register.value >> ((attribute_index % 4) * 8)) & 0xFF
    outer = attribute >> 4
    inner = attribute & 0xF
    if outer == 0 and inner in (0x0, 0x4, 0x8, 0xC):
        return MpuMemoryAttributes(
            MpuMemoryType.DEVICE,
            shareable,
            False,
            bool(inner & 0x4),
            attribute_index,
            attribute,
            "device memory",
        )
    if outer != 0:
        return MpuMemoryAttributes(
            MpuMemoryType.NORMAL,
            shareable,
            outer != 0x4 or inner != 0x4,
            None,
            attribute_index,
            attribute,
            "normal memory",
        )
    return MpuMemoryAttributes(
        MpuMemoryType.UNKNOWN,
        shareable,
        None,
        None,
        attribute_index,
        attribute,
        "reserved PMSAv8 MAIR attribute",
    )
