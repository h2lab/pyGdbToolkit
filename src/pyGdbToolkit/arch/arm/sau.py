# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Cortex-M Secure Attribution Unit inspection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING

from ...target_memory import TargetMemory, TargetReadError, TargetWriteError, WritableTargetMemory
from ..base import RegisterValue

if TYPE_CHECKING:
    from .cortex_m import CortexMTargetDescription


SAU_BASE_ADDRESS = 0xE000EDD0


class SauRegister(IntEnum):
    """SAU register offsets from ``SAU_BASE_ADDRESS``."""

    CTRL = 0x000
    TYPE = 0x004
    RNR = 0x008
    RBAR = 0x00C
    RLAR = 0x010


class SauDefaultSecurity(StrEnum):
    """Security attribution outside enabled SAU regions."""

    SECURE = "secure"
    NON_SECURE = "non-secure"


class SauAttribution(StrEnum):
    """Security attribution selected by one enabled SAU region."""

    NON_SECURE = "non-secure"
    NON_SECURE_CALLABLE = "non-secure-callable"


@dataclass(frozen=True)
class SauDescription:
    """Static SAU interface description for one Armv8-M core."""

    registers: tuple[SauRegister, ...]


@dataclass(frozen=True)
class SauStatus:
    """SAU implementation and enable-state information."""

    description: SauDescription | None
    registers: tuple[RegisterValue, ...]
    region_count: int | None
    enabled: bool | None
    all_non_secure: bool | None
    default_security: SauDefaultSecurity | None
    unavailable_reason: str | None = None

    @property
    def is_available(self) -> bool:
        """Whether the SAU status was read successfully."""
        return self.unavailable_reason is None

    @classmethod
    def unavailable(
        cls,
        description: SauDescription | None,
        reason: str,
        registers: tuple[RegisterValue, ...] = (),
    ) -> SauStatus:
        """Create an explicit unavailable SAU status."""
        return cls(description, registers, None, None, None, None, reason)

    def get_register(self, name: str) -> RegisterValue | None:
        """Find a captured SAU register by case-insensitive name."""
        normalized_name = name.upper()
        return next(
            (register for register in self.registers if register.name.upper() == normalized_name),
            None,
        )


@dataclass(frozen=True)
class SauRegion:
    """One decoded SAU region configuration."""

    index: int
    enabled: bool
    start_address: int
    end_address: int
    size_bytes: int | None
    attribution: SauAttribution
    raw_rbar: int
    raw_rlar: int


@dataclass(frozen=True)
class SauRegionResult:
    """A decoded SAU region or its explicit selection/read failure."""

    index: int
    region: SauRegion | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        """Require exactly one region result state."""
        if (self.region is None) == (self.unavailable_reason is None):
            raise ValueError("an SAU region result must be available or unavailable")

    @classmethod
    def unavailable(cls, index: int, reason: str) -> SauRegionResult:
        """Create an unavailable SAU region result."""
        return cls(index=index, unavailable_reason=reason)

    @property
    def is_available(self) -> bool:
        """Whether the selected region was read and decoded."""
        return self.region is not None


@dataclass(frozen=True)
class SauDump:
    """SAU status and region results for one Cortex-M target."""

    status: SauStatus
    regions: tuple[SauRegionResult, ...]
    restore_error: str | None = None


ARMV8_M_SAU = SauDescription(tuple(SauRegister))


def read_sau_status(reader: TargetMemory, target: CortexMTargetDescription) -> SauStatus:
    """Read SAU implementation and control status for a known Cortex-M core."""
    description = target.core.sau if target.core is not None else None
    if description is None:
        return SauStatus.unavailable(None, "this Cortex-M core has no SAU description")

    registers: list[RegisterValue] = []
    sau_type = _read_register(reader, SauRegister.TYPE)
    registers.append(sau_type)
    if not sau_type.is_available:
        assert sau_type.unavailable_reason is not None
        return SauStatus.unavailable(description, sau_type.unavailable_reason, tuple(registers))

    sau_ctrl = _read_register(reader, SauRegister.CTRL)
    registers.append(sau_ctrl)
    if not sau_ctrl.is_available:
        assert sau_ctrl.unavailable_reason is not None
        return SauStatus.unavailable(description, sau_ctrl.unavailable_reason, tuple(registers))

    assert sau_type.value is not None
    assert sau_ctrl.value is not None
    all_non_secure = bool(sau_ctrl.value & (1 << 1))
    return SauStatus(
        description,
        tuple(registers),
        sau_type.value & 0xFF,
        bool(sau_ctrl.value & 0x1),
        all_non_secure,
        SauDefaultSecurity.NON_SECURE if all_non_secure else SauDefaultSecurity.SECURE,
    )


def dump_sau_regions(reader: WritableTargetMemory, target: CortexMTargetDescription) -> SauDump:
    """Read and decode SAU regions, restoring the target's original selector."""
    status = read_sau_status(reader, target)
    if not status.is_available or status.region_count == 0:
        return SauDump(status, ())

    original_rnr = _read_register(reader, SauRegister.RNR)
    if not original_rnr.is_available:
        assert original_rnr.unavailable_reason is not None
        return SauDump(
            SauStatus.unavailable(
                status.description,
                original_rnr.unavailable_reason,
                status.registers + (original_rnr,),
            ),
            (),
        )

    assert original_rnr.value is not None
    regions: list[SauRegionResult] = []
    restore_error: str | None = None
    try:
        assert status.region_count is not None
        for index in range(status.region_count):
            try:
                reader.write_uint32(SAU_BASE_ADDRESS + SauRegister.RNR, index)
                rbar = reader.read_uint32(SAU_BASE_ADDRESS + SauRegister.RBAR)
                rlar = reader.read_uint32(SAU_BASE_ADDRESS + SauRegister.RLAR)
            except (TargetReadError, TargetWriteError) as error:
                regions.append(SauRegionResult.unavailable(index, str(error)))
                continue
            regions.append(SauRegionResult(index, _decode_region(index, rbar, rlar)))
    finally:
        try:
            reader.write_uint32(SAU_BASE_ADDRESS + SauRegister.RNR, original_rnr.value)
        except TargetWriteError as error:
            restore_error = str(error)
    return SauDump(status, tuple(regions), restore_error)


def _read_register(reader: TargetMemory, register: SauRegister) -> RegisterValue:
    """Read one SAU register while preserving a target access failure."""
    address = SAU_BASE_ADDRESS + register.value
    try:
        value = reader.read_uint32(address)
    except TargetReadError as error:
        return RegisterValue.unavailable(register.name, address, 32, str(error))
    return RegisterValue.known(register.name, address, 32, value)


def _decode_region(index: int, rbar: int, rlar: int) -> SauRegion:
    """Decode one SAU ``RBAR``/``RLAR`` region pair."""
    start_address = rbar & 0xFFFFFFE0
    end_address = (rlar & 0xFFFFFFE0) | 0x1F
    return SauRegion(
        index,
        bool(rlar & 0x1),
        start_address,
        end_address,
        end_address - start_address + 1 if end_address >= start_address else None,
        SauAttribution.NON_SECURE_CALLABLE if rlar & (1 << 1) else SauAttribution.NON_SECURE,
        rbar,
        rlar,
    )
