# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Tests for security-relevant AArch64 feature-field evidence."""

from __future__ import annotations

import pytest

from pyGdbToolkit.arch.aarch64.cpu import CpuRegister
from pyGdbToolkit.arch.aarch64.features import (
    FEATURE_FIELDS,
    FEATURE_REGISTERS,
    FeatureField,
    FeatureSupport,
    collect_feature_report,
    decode_feature_report,
)


@pytest.mark.parametrize("field", FEATURE_FIELDS, ids=lambda field: field.name)
@pytest.mark.parametrize("encoding", range(16))
def test_all_field_encodings(field: FeatureField, encoding: int) -> None:
    """Only explicitly recognized positive encodings imply support."""
    report = decode_feature_report((CpuRegister(field.register, 64, encoding << field.shift),))
    capability = report.capability(field.name)
    expected = (
        FeatureSupport.ABSENT
        if encoding == 0
        else FeatureSupport.PRESENT if encoding <= len(field.levels) else FeatureSupport.UNKNOWN
    )
    assert capability.support is expected
    assert capability.encoding == encoding
    if expected is FeatureSupport.PRESENT:
        assert capability.description == field.levels[encoding - 1]


@pytest.mark.parametrize(
    "name,register,shift,encoding,level",
    (
        ("PAN", "ID_AA64MMFR1_EL1", 20, 2, "PAN2"),
        ("VHE", "ID_AA64MMFR1_EL1", 8, 1, "VHE"),
        ("HPDS", "ID_AA64MMFR1_EL1", 12, 2, "HPDS2"),
        ("HAFDBS", "ID_AA64MMFR1_EL1", 0, 2, "Hardware access flag and dirty state"),
        ("UAO", "ID_AA64MMFR2_EL1", 4, 1, "UAO"),
        ("S2FWB", "ID_AA64MMFR2_EL1", 40, 1, "S2FWB"),
        ("PAC address QARMA5", "ID_AA64ISAR1_EL1", 4, 1, "PAuth"),
        ("PAC address implementation-defined", "ID_AA64ISAR1_EL1", 8, 3, "PAuth2"),
        ("PAC address QARMA3", "ID_AA64ISAR2_EL1", 12, 4, "PAuth2 with FPAC"),
        ("PAC generic QARMA5", "ID_AA64ISAR1_EL1", 24, 1, "PACGA with QARMA5"),
        (
            "PAC generic implementation-defined",
            "ID_AA64ISAR1_EL1",
            28,
            1,
            "PACGA with implementation-defined algorithm",
        ),
        ("PAC generic QARMA3", "ID_AA64ISAR2_EL1", 8, 1, "PACGA with QARMA3"),
        ("BTI", "ID_AA64PFR1_EL1", 0, 1, "BTI"),
        ("SSBS", "ID_AA64PFR1_EL1", 4, 2, "SSBS2"),
        (
            "MTE",
            "ID_AA64PFR1_EL1",
            8,
            1,
            "MTE instructions only; no allocation tags or tag checking",
        ),
        ("FGT", "ID_AA64MMFR0_EL1", 56, 2, "FGT2"),
        ("ECV", "ID_AA64MMFR0_EL1", 60, 2, "ECV with CNTPOFF_EL2"),
        ("HCX", "ID_AA64MMFR1_EL1", 40, 1, "HCX"),
        ("CMOW", "ID_AA64MMFR1_EL1", 56, 1, "CMOW"),
    ),
)
def test_architected_field_positions(
    name: str,
    register: str,
    shift: int,
    encoding: int,
    level: str,
) -> None:
    """Independent register vectors fix field locations and meaningful variants."""
    capability = decode_feature_report((CpuRegister(register, 64, encoding << shift),)).capability(
        name
    )
    assert capability.support is FeatureSupport.PRESENT
    assert capability.description == level


def test_missing_registers_are_not_zero_registers() -> None:
    """Unavailable registers do not claim that optional capabilities are absent."""
    missing = decode_feature_report(())
    zeros = decode_feature_report(tuple(CpuRegister(name, 64, 0) for name in FEATURE_REGISTERS))
    assert all(capability.support is FeatureSupport.UNKNOWN for capability in missing.capabilities)
    assert all(capability.support is FeatureSupport.ABSENT for capability in zeros.capabilities)
    assert missing.observed_generations == zeros.observed_generations == ()


def test_partial_registers_do_not_fabricate_high_fields() -> None:
    """A low-word read cannot establish HCX, CMOW, FGT or ECV."""
    report = decode_feature_report(
        (
            CpuRegister("ID_AA64MMFR1_EL1", 64, 1 << 20, 32, "partial source"),
            CpuRegister("ID_AA64MMFR0_EL1", 64, 0, 32, "partial source"),
        )
    )
    assert report.capability("PAN").support is FeatureSupport.PRESENT
    for name in ("HCX", "CMOW", "FGT", "ECV"):
        capability = report.capability(name)
        assert capability.support is FeatureSupport.UNKNOWN
        assert capability.encoding is None
        assert capability.source == "partial source"


def test_partial_nibble_and_register_width_are_respected() -> None:
    """Both the register width and valid bit count bound the evidence."""
    for width, valid in ((64, 23), (23, 64)):
        report = decode_feature_report((CpuRegister("ID_AA64MMFR1_EL1", width, 1 << 20, valid),))
        assert report.capability("PAN").support is FeatureSupport.UNKNOWN


def test_signed_register_value_keeps_high_fields() -> None:
    """A signed GDB integer does not lose bits 63:60."""
    value = (1 << 60) | (1 << 56) | (1 << 63)
    report = decode_feature_report((CpuRegister("ID_AA64MMFR0_EL1", 64, value - (1 << 64)),))
    assert report.capability("FGT").support is FeatureSupport.PRESENT
    assert report.capability("ECV").encoding == 9
    assert report.capability("ECV").support is FeatureSupport.UNKNOWN


def test_family_generations_are_not_a_cpu_version() -> None:
    """Feature history retains each observed generation without certifying a release."""
    report = decode_feature_report(
        (
            CpuRegister("ID_AA64MMFR1_EL1", 64, (1 << 20) | (1 << 40)),
            CpuRegister("ID_AA64PFR1_EL1", 64, 1),
        )
    )
    assert report.observed_generations == ("ARMv8.1-A", "ARMv8.5-A", "ARMv8.7-A")


def test_collection_reads_each_named_register_once_and_is_fresh() -> None:
    """ID collection uses portable aliases only, with no cached capability state."""

    class Reader:
        """Record the collector's register requests."""

        value = 0

        def __init__(self) -> None:
            """Start with an empty request log."""
            self.calls: list[tuple[str, ...]] = []

        def read_first(self, names: tuple[str, ...]) -> int | None:
            """Expose the current ID value under the requested register."""
            self.calls.append(names)
            return self.value

    reader = Reader()
    first = collect_feature_report(reader)
    assert reader.calls == [(name.lower(), name) for name in FEATURE_REGISTERS]
    reader.value = 1
    second = collect_feature_report(reader)
    assert first.capability("BTI").support is FeatureSupport.ABSENT
    assert second.capability("BTI").support is FeatureSupport.PRESENT
    assert len(reader.calls) == 2 * len(FEATURE_REGISTERS)
