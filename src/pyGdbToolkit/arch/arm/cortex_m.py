# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Cortex-M core descriptions, CPUID decoding, and SCB inspection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum

from ...target_memory import TargetMemory, TargetReadError
from ..base import Architecture, ProbeResult, RegisterValue, SystemRegisterSet, TargetDescription
from .mpu import MpuDescription, PMSA_V7_MPU, PMSA_V8_MPU, PMSA_V8_PXN_MPU
from .target import ArmProfile, ArmTargetDescription

SCB_BASE_ADDRESS = 0xE000ED00
CPUID_ADDRESS = SCB_BASE_ADDRESS
ARM_IMPLEMENTER = 0x41


class CortexMPart(IntEnum):
    """Architected Cortex-M CPUID part numbers."""

    M0 = 0xC20
    M1 = 0xC21
    M3 = 0xC23
    M4 = 0xC24
    M7 = 0xC27
    M0_PLUS = 0xC60
    M23 = 0xD20
    M33 = 0xD21
    M55 = 0xD22
    M85 = 0xD23
    M35P = 0xD31
    M52 = 0xD32


class CortexMArchitecture(StrEnum):
    """Cortex-M architecture versions represented by the core catalog."""

    ARMV6_M = "Armv6-M"
    ARMV7_M = "Armv7-M"
    ARMV7E_M = "Armv7E-M"
    ARMV8_M_BASELINE = "Armv8-M Baseline"
    ARMV8_M_MAINLINE = "Armv8-M Mainline"
    ARMV8_1_M_MAINLINE = "Armv8.1-M Mainline"


class CortexMFeature(StrEnum):
    """Architectural feature categories used to describe Cortex-M cores."""

    CONFIGURABLE_FAULTS = "configurable-faults"
    DSP_EXTENSION = "dsp-extension"
    SECURITY_EXTENSION = "security-extension"
    CACHE_CONTROL = "cache-control"
    AHB_SLAVE_CONTROL = "ahb-slave-control"
    RAS_FAULT_STATUS = "ras-fault-status"


class ScbRegister(IntEnum):
    """System Control Block register offsets from ``SCB_BASE_ADDRESS``."""

    CPUID = 0x000
    ICSR = 0x004
    VTOR = 0x008
    AIRCR = 0x00C
    SCR = 0x010
    CCR = 0x014
    SHPR1 = 0x018
    SHPR2 = 0x01C
    SHPR3 = 0x020
    SHCSR = 0x024
    CFSR = 0x028
    HFSR = 0x02C
    DFSR = 0x030
    MMFAR = 0x034
    BFAR = 0x038
    AFSR = 0x03C
    ID_PFR0 = 0x040
    ID_PFR1 = 0x044
    ID_DFR0 = 0x048
    ID_AFR0 = 0x04C
    ID_MMFR0 = 0x050
    ID_MMFR1 = 0x054
    ID_MMFR2 = 0x058
    ID_MMFR3 = 0x05C
    ID_ISAR0 = 0x060
    ID_ISAR1 = 0x064
    ID_ISAR2 = 0x068
    ID_ISAR3 = 0x06C
    ID_ISAR4 = 0x070
    ID_ISAR5 = 0x074
    CLIDR = 0x078
    CTR = 0x07C
    CCSIDR = 0x080
    CSSELR = 0x084
    CPACR = 0x088
    NSACR = 0x08C
    SFSR = 0x0E4
    SFAR = 0x0E8
    RFSR = 0x204
    AHBSCR = 0x2A0


@dataclass(frozen=True)
class CortexMCoreDescription:
    """Immutable architectural description for one Cortex-M CPUID part."""

    part: CortexMPart
    core_name: str
    architecture_version: CortexMArchitecture
    features: frozenset[CortexMFeature]
    scb_registers: tuple[ScbRegister, ...]
    mpu: MpuDescription | None


@dataclass(frozen=True)
class CortexMTargetDescription(ArmTargetDescription):
    """Decoded Cortex-M target identity from its architected CPUID register."""

    raw_cpuid: int
    implementer: int
    variant: int
    cpuid_architecture: int
    part_number: int
    patch: int
    core: CortexMCoreDescription | None

    @property
    def rnp_revision(self) -> str:
        """Return the standard Arm rNp revision notation."""
        return f"r{self.variant}p{self.patch}"


_BASELINE_SCB = (
    ScbRegister.CPUID,
    ScbRegister.ICSR,
    ScbRegister.AIRCR,
    ScbRegister.SCR,
    ScbRegister.CCR,
    ScbRegister.SHPR2,
    ScbRegister.SHPR3,
    ScbRegister.SHCSR,
)
_BASELINE_WITH_VTOR_SCB = _BASELINE_SCB[:2] + (ScbRegister.VTOR,) + _BASELINE_SCB[2:]
_MAINLINE_SCB = (
    ScbRegister.CPUID,
    ScbRegister.ICSR,
    ScbRegister.VTOR,
    ScbRegister.AIRCR,
    ScbRegister.SCR,
    ScbRegister.CCR,
    ScbRegister.SHPR1,
    ScbRegister.SHPR2,
    ScbRegister.SHPR3,
    ScbRegister.SHCSR,
    ScbRegister.CFSR,
    ScbRegister.HFSR,
    ScbRegister.DFSR,
    ScbRegister.MMFAR,
    ScbRegister.BFAR,
    ScbRegister.AFSR,
    ScbRegister.ID_PFR0,
    ScbRegister.ID_PFR1,
    ScbRegister.ID_DFR0,
    ScbRegister.ID_AFR0,
    ScbRegister.ID_MMFR0,
    ScbRegister.ID_MMFR1,
    ScbRegister.ID_MMFR2,
    ScbRegister.ID_MMFR3,
    ScbRegister.ID_ISAR0,
    ScbRegister.ID_ISAR1,
    ScbRegister.ID_ISAR2,
    ScbRegister.ID_ISAR3,
    ScbRegister.ID_ISAR4,
    ScbRegister.CPACR,
)
_MAINLINE_WITH_CACHE_SCB = _MAINLINE_SCB + (
    ScbRegister.CLIDR,
    ScbRegister.CTR,
    ScbRegister.CCSIDR,
    ScbRegister.CSSELR,
    ScbRegister.AHBSCR,
)
_MAINLINE_WITH_CACHES_SCB = _MAINLINE_SCB + (
    ScbRegister.CLIDR,
    ScbRegister.CTR,
    ScbRegister.CCSIDR,
    ScbRegister.CSSELR,
)
_V8_MAINLINE_CORE_SCB = _MAINLINE_SCB[:-1] + (
    ScbRegister.ID_ISAR5,
    ScbRegister.CPACR,
)
_V8_MAINLINE_SCB = _V8_MAINLINE_CORE_SCB + (
    ScbRegister.NSACR,
    ScbRegister.SFSR,
    ScbRegister.SFAR,
)
_V8_1_MAINLINE_SCB = _V8_MAINLINE_CORE_SCB + (
    ScbRegister.CLIDR,
    ScbRegister.CTR,
    ScbRegister.CCSIDR,
    ScbRegister.CSSELR,
    ScbRegister.NSACR,
    ScbRegister.SFSR,
    ScbRegister.SFAR,
    ScbRegister.RFSR,
)

CORTEX_M_CORES: dict[CortexMPart, CortexMCoreDescription] = {
    CortexMPart.M0: CortexMCoreDescription(
        CortexMPart.M0,
        "Cortex-M0",
        CortexMArchitecture.ARMV6_M,
        frozenset(),
        _BASELINE_SCB,
        None,
    ),
    CortexMPart.M1: CortexMCoreDescription(
        CortexMPart.M1,
        "Cortex-M1",
        CortexMArchitecture.ARMV6_M,
        frozenset(),
        _BASELINE_SCB,
        None,
    ),
    CortexMPart.M0_PLUS: CortexMCoreDescription(
        CortexMPart.M0_PLUS,
        "Cortex-M0+",
        CortexMArchitecture.ARMV6_M,
        frozenset(),
        _BASELINE_WITH_VTOR_SCB,
        None,
    ),
    CortexMPart.M3: CortexMCoreDescription(
        CortexMPart.M3,
        "Cortex-M3",
        CortexMArchitecture.ARMV7_M,
        frozenset({CortexMFeature.CONFIGURABLE_FAULTS}),
        _MAINLINE_SCB,
        PMSA_V7_MPU,
    ),
    CortexMPart.M4: CortexMCoreDescription(
        CortexMPart.M4,
        "Cortex-M4",
        CortexMArchitecture.ARMV7E_M,
        frozenset({CortexMFeature.CONFIGURABLE_FAULTS, CortexMFeature.DSP_EXTENSION}),
        _MAINLINE_SCB,
        PMSA_V7_MPU,
    ),
    CortexMPart.M7: CortexMCoreDescription(
        CortexMPart.M7,
        "Cortex-M7",
        CortexMArchitecture.ARMV7E_M,
        frozenset(
            {
                CortexMFeature.CONFIGURABLE_FAULTS,
                CortexMFeature.DSP_EXTENSION,
                CortexMFeature.CACHE_CONTROL,
                CortexMFeature.AHB_SLAVE_CONTROL,
            }
        ),
        _MAINLINE_WITH_CACHE_SCB,
        PMSA_V7_MPU,
    ),
    CortexMPart.M23: CortexMCoreDescription(
        CortexMPart.M23,
        "Cortex-M23",
        CortexMArchitecture.ARMV8_M_BASELINE,
        frozenset({CortexMFeature.SECURITY_EXTENSION}),
        _BASELINE_WITH_VTOR_SCB,
        PMSA_V8_MPU,
    ),
    CortexMPart.M33: CortexMCoreDescription(
        CortexMPart.M33,
        "Cortex-M33",
        CortexMArchitecture.ARMV8_M_MAINLINE,
        frozenset({CortexMFeature.CONFIGURABLE_FAULTS, CortexMFeature.SECURITY_EXTENSION}),
        _V8_MAINLINE_SCB,
        PMSA_V8_MPU,
    ),
    CortexMPart.M35P: CortexMCoreDescription(
        CortexMPart.M35P,
        "Cortex-M35P",
        CortexMArchitecture.ARMV8_M_MAINLINE,
        frozenset({CortexMFeature.CONFIGURABLE_FAULTS, CortexMFeature.SECURITY_EXTENSION}),
        _V8_MAINLINE_SCB,
        PMSA_V8_MPU,
    ),
    CortexMPart.M52: CortexMCoreDescription(
        CortexMPart.M52,
        "Cortex-M52",
        CortexMArchitecture.ARMV8_1_M_MAINLINE,
        frozenset(
            {
                CortexMFeature.CONFIGURABLE_FAULTS,
                CortexMFeature.SECURITY_EXTENSION,
                CortexMFeature.CACHE_CONTROL,
                CortexMFeature.RAS_FAULT_STATUS,
            }
        ),
        _V8_1_MAINLINE_SCB,
        PMSA_V8_PXN_MPU,
    ),
    CortexMPart.M55: CortexMCoreDescription(
        CortexMPart.M55,
        "Cortex-M55",
        CortexMArchitecture.ARMV8_1_M_MAINLINE,
        frozenset(
            {
                CortexMFeature.CONFIGURABLE_FAULTS,
                CortexMFeature.SECURITY_EXTENSION,
                CortexMFeature.CACHE_CONTROL,
                CortexMFeature.RAS_FAULT_STATUS,
            }
        ),
        _V8_1_MAINLINE_SCB,
        PMSA_V8_PXN_MPU,
    ),
    CortexMPart.M85: CortexMCoreDescription(
        CortexMPart.M85,
        "Cortex-M85",
        CortexMArchitecture.ARMV8_1_M_MAINLINE,
        frozenset(
            {
                CortexMFeature.CONFIGURABLE_FAULTS,
                CortexMFeature.SECURITY_EXTENSION,
                CortexMFeature.CACHE_CONTROL,
                CortexMFeature.RAS_FAULT_STATUS,
            }
        ),
        _V8_1_MAINLINE_SCB,
        PMSA_V8_PXN_MPU,
    ),
}


def decode_cpuid(raw: int) -> CortexMTargetDescription:
    """Decode an architected Cortex-M CPUID register value.

    Parameters
    ----------
    raw
        The unsigned 32-bit CPUID value from the System Control Block.

    Returns
    -------
    CortexMTargetDescription
        Core identity, revision, and a registered core description when known.

    Raises
    ------
    ValueError
        If ``raw`` is outside the unsigned 32-bit CPUID range.
    """
    if not 0 <= raw <= 0xFFFFFFFF:
        raise ValueError("CPUID must be an unsigned 32-bit value")

    implementer = raw >> 24
    variant = (raw >> 20) & 0xF
    cpuid_architecture = (raw >> 16) & 0xF
    part_number = (raw >> 4) & 0xFFF
    patch = raw & 0xF
    part = _cortex_m_part(part_number)
    core = CORTEX_M_CORES.get(part) if part is not None else None
    core_name = core.core_name if core is not None else f"Unknown Cortex-M part 0x{part_number:03X}"
    return CortexMTargetDescription(
        architecture=Architecture.ARM,
        family="Arm",
        core_name=core_name,
        revision=f"r{variant}p{patch}",
        profile=ArmProfile.CORTEX_M,
        raw_cpuid=raw,
        implementer=implementer,
        variant=variant,
        cpuid_architecture=cpuid_architecture,
        part_number=part_number,
        patch=patch,
        core=core,
    )


def read_scb(
    reader: TargetMemory,
    target: CortexMTargetDescription,
    *,
    cpuid_value: int | None = None,
) -> SystemRegisterSet:
    """Read the SCB registers applicable to a known Cortex-M core.

    Parameters
    ----------
    reader
        Target-memory reader used for 32-bit SCB accesses.
    target
        Cortex-M description returned by :func:`decode_cpuid`.
    cpuid_value
        Optional already-read CPUID value used to avoid a duplicate target read.

    Returns
    -------
    SystemRegisterSet
        An ``SCB`` block containing available values and individual read errors.
    """
    if target.core is None:
        return SystemRegisterSet(Architecture.ARM, "SCB", ())

    register_values: list[RegisterValue] = []
    for register in target.core.scb_registers:
        address = SCB_BASE_ADDRESS + register.value
        if register is ScbRegister.CPUID and cpuid_value is not None:
            register_values.append(RegisterValue.known(register.name, address, 32, cpuid_value))
            continue
        try:
            value = reader.read_uint32(address)
        except TargetReadError as error:
            register_values.append(
                RegisterValue.unavailable(register.name, address, 32, str(error))
            )
        else:
            register_values.append(RegisterValue.known(register.name, address, 32, value))
    return SystemRegisterSet(Architecture.ARM, "SCB", tuple(register_values))


def inspect_cortex_m(reader: TargetMemory) -> tuple[CortexMTargetDescription, SystemRegisterSet]:
    """Read CPUID then inspect the SCB register set selected by its core part.

    Parameters
    ----------
    reader
        Target-memory reader used for CPUID and SCB accesses.

    Returns
    -------
    tuple[CortexMTargetDescription, SystemRegisterSet]
        Decoded target description and its known-core SCB register results.
    """
    raw_cpuid = reader.read_uint32(CPUID_ADDRESS)
    target = decode_cpuid(raw_cpuid)
    return target, read_scb(reader, target, cpuid_value=raw_cpuid)


class CortexMProbe:
    """Registry adapter for recognized Cortex-M CPUID parts."""

    architecture = Architecture.ARM

    def probe(self, reader: TargetMemory) -> ProbeResult:
        """Recognize a known Cortex-M part from CPUID."""
        try:
            target = decode_cpuid(reader.read_uint32(CPUID_ADDRESS))
        except TargetReadError as error:
            return ProbeResult.unavailable(f"could not read Cortex-M CPUID: {error}")
        if target.implementer != ARM_IMPLEMENTER or target.cpuid_architecture not in (0xC, 0xF):
            return ProbeResult.unavailable("CPUID does not identify an Arm Cortex-M architecture")
        if target.core is None:
            return ProbeResult.unavailable(
                f"unrecognized Cortex-M CPUID part 0x{target.part_number:03X}"
            )
        return ProbeResult.detected(target)

    def read_system_registers(
        self,
        reader: TargetMemory,
        target: TargetDescription,
    ) -> SystemRegisterSet:
        """Read SCB registers for a target returned by this probe."""
        if not isinstance(target, CortexMTargetDescription):
            raise ValueError("CortexMProbe requires a CortexMTargetDescription")
        return read_scb(reader, target)


def _cortex_m_part(part_number: int) -> CortexMPart | None:
    """Convert a raw 12-bit part number to its enum member when known."""
    try:
        return CortexMPart(part_number)
    except ValueError:
        return None
