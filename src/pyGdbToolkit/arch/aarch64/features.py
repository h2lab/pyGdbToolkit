# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Decode observed AArch64 security capabilities without inferring a CPU version."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..diagnostics import DiagnosticRegisterReader
from .cpu import CpuRegister


class FeatureSupport(StrEnum):
    """Separate an unobservable capability from a capability reported absent."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FeatureField:
    """Describe a four-bit ID field and its explicitly recognized positive levels."""

    name: str
    register: str
    field: str
    shift: int
    introduced_in: str
    levels: tuple[str, ...]


_PAUTH_LEVELS = (
    "PAuth",
    "PAuth with EPAC",
    "PAuth2",
    "PAuth2 with FPAC",
    "PAuth2 with FPAC and FPACCOMBINE",
    "PAuth2 with FPAC, FPACCOMBINE and PAuth_LR",
)

FEATURE_FIELDS = (
    FeatureField("PAN", "ID_AA64MMFR1_EL1", "PAN", 20, "ARMv8.1-A", ("PAN", "PAN2", "PAN3")),
    FeatureField("VHE", "ID_AA64MMFR1_EL1", "VH", 8, "ARMv8.1-A", ("VHE",)),
    FeatureField("HPDS", "ID_AA64MMFR1_EL1", "HPDS", 12, "ARMv8.1-A", ("HPDS", "HPDS2")),
    FeatureField(
        "HAFDBS",
        "ID_AA64MMFR1_EL1",
        "HAFDBS",
        0,
        "ARMv8.1-A",
        ("Hardware access flag", "Hardware access flag and dirty state", "HAFT", "HDBSS"),
    ),
    FeatureField("UAO", "ID_AA64MMFR2_EL1", "UAO", 4, "ARMv8.2-A", ("UAO",)),
    FeatureField("S2FWB", "ID_AA64MMFR2_EL1", "FWB", 40, "ARMv8.4-A", ("S2FWB",)),
    FeatureField("PAC address QARMA5", "ID_AA64ISAR1_EL1", "APA", 4, "ARMv8.3-A", _PAUTH_LEVELS),
    FeatureField(
        "PAC address implementation-defined",
        "ID_AA64ISAR1_EL1",
        "API",
        8,
        "ARMv8.3-A",
        _PAUTH_LEVELS,
    ),
    FeatureField("PAC address QARMA3", "ID_AA64ISAR2_EL1", "APA3", 12, "ARMv8.3-A", _PAUTH_LEVELS),
    FeatureField(
        "PAC generic QARMA5", "ID_AA64ISAR1_EL1", "GPA", 24, "ARMv8.3-A", ("PACGA with QARMA5",)
    ),
    FeatureField(
        "PAC generic implementation-defined",
        "ID_AA64ISAR1_EL1",
        "GPI",
        28,
        "ARMv8.3-A",
        ("PACGA with implementation-defined algorithm",),
    ),
    FeatureField(
        "PAC generic QARMA3", "ID_AA64ISAR2_EL1", "GPA3", 8, "ARMv8.3-A", ("PACGA with QARMA3",)
    ),
    FeatureField("BTI", "ID_AA64PFR1_EL1", "BT", 0, "ARMv8.5-A", ("BTI",)),
    FeatureField("SSBS", "ID_AA64PFR1_EL1", "SSBS", 4, "ARMv8.5-A", ("SSBS", "SSBS2")),
    FeatureField(
        "MTE",
        "ID_AA64PFR1_EL1",
        "MTE",
        8,
        "ARMv8.5-A",
        (
            "MTE instructions only; no allocation tags or tag checking",
            "MTE2: allocation tags and synchronous tag checking",
            "MTE3: asymmetric tag fault handling",
        ),
    ),
    FeatureField("FGT", "ID_AA64MMFR0_EL1", "FGT", 56, "ARMv8.6-A", ("FGT", "FGT2")),
    FeatureField(
        "ECV", "ID_AA64MMFR0_EL1", "ECV", 60, "ARMv8.6-A", ("ECV", "ECV with CNTPOFF_EL2")
    ),
    FeatureField("HCX", "ID_AA64MMFR1_EL1", "HCX", 40, "ARMv8.7-A", ("HCX",)),
    FeatureField("CMOW", "ID_AA64MMFR1_EL1", "CMOW", 56, "ARMv8.8-A", ("CMOW",)),
)

FEATURE_REGISTERS = tuple(dict.fromkeys(field.register for field in FEATURE_FIELDS))


@dataclass(frozen=True)
class FeatureCapability:
    """Retain the observed field, level and evidence for a security capability."""

    field: FeatureField
    support: FeatureSupport
    encoding: int | None
    description: str
    source: str


@dataclass(frozen=True)
class FeatureReport:
    """A fresh capability snapshot, not certification of an architecture release."""

    registers: tuple[CpuRegister, ...]
    capabilities: tuple[FeatureCapability, ...]

    def capability(self, name: str) -> FeatureCapability:
        """Return the capability identified by its stable family name."""
        return next(capability for capability in self.capabilities if capability.field.name == name)

    @property
    def observed_generations(self) -> tuple[str, ...]:
        """List family introduction generations, not a minimum or exact CPU version."""
        return tuple(
            sorted(
                {
                    capability.field.introduced_in
                    for capability in self.capabilities
                    if capability.support is FeatureSupport.PRESENT
                }
            )
        )


def decode_feature_report(registers: tuple[CpuRegister, ...]) -> FeatureReport:
    """Decode known fields only when all four field bits are available."""
    by_name = {register.name: register for register in registers}
    capabilities: list[FeatureCapability] = []
    for field in FEATURE_FIELDS:
        register = by_name.get(field.register)
        encoding = None
        support = FeatureSupport.UNKNOWN
        source = register.source if register is not None else "unavailable"
        description = "Register is not exposed or readable."
        if register is not None and register.value is not None:
            if min(register.valid_bits, register.width_bits) < field.shift + 4:
                description = "Register evidence does not cover all field bits."
            else:
                encoding = (register.value >> field.shift) & 0xF
                if encoding == 0:
                    support = FeatureSupport.ABSENT
                    description = "Not implemented according to the observed ID field."
                elif encoding <= len(field.levels):
                    support = FeatureSupport.PRESENT
                    description = field.levels[encoding - 1]
                else:
                    description = "Reserved or unrecognized encoding; support is unknown."
        capabilities.append(FeatureCapability(field, support, encoding, description, source))
    return FeatureReport(registers, tuple(capabilities))


def collect_feature_report(reader: DiagnosticRegisterReader | None) -> FeatureReport:
    """Read each ID register once through named runtime aliases, without fallback probes."""
    registers = tuple(
        CpuRegister(
            name,
            64,
            None if reader is None else reader.read_first((name.lower(), name)),
        )
        for name in FEATURE_REGISTERS
    )
    return decode_feature_report(registers)
