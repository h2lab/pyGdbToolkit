# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Arm Cortex-M security diagnostic service."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from pyGdbToolkit.arch.arm.cortex_m import decode_cpuid
from pyGdbToolkit.arch.arm.models import DeviceReport, FieldValue
from pyGdbToolkit.arch.arm.security import CortexMSecurityAuditor
from pyGdbToolkit.target_memory import TargetReadError


@dataclass
class Memory:
    """Deterministic writable target-memory fixture."""

    values: dict[int, int]
    writes: list[tuple[int, int]] = field(default_factory=list)

    def read_bytes(self, address: int, size: int) -> bytes:
        """Return an encoded unsigned value for the requested test range."""
        return self.read_uint32(address).to_bytes(size, "little")

    def read_uint16(self, address: int) -> int:
        """Read a test halfword."""
        return self.read_uint32(address) & 0xFFFF

    def read_uint32(self, address: int) -> int:
        """Read one configured word or raise the production access error."""
        try:
            return self.values[address]
        except KeyError as error:
            raise TargetReadError(address, 4, "not mapped") from error

    def write_uint32(self, address: int, value: int) -> None:
        """Record and retain MPU/SAU selector writes."""
        self.writes.append((address, value))
        self.values[address] = value


@dataclass
class Registers:
    """Deterministic core-register fixture."""

    values: dict[str, int]

    def read_first(self, names: tuple[str, ...]) -> int | None:
        """Return the first configured candidate register."""
        return next((self.values[name] for name in names if name in self.values), None)


def _memory(mpu_control: int = 1, subregion_disable_mask: int = 0) -> Memory:
    """Build a Cortex-M4 target with one non-executable SRAM MPU region."""
    values = {
        0xE000ED00: 0x410FC240,
        0xE000ED08: 0x08000000,
        0xE000ED14: (1 << 3) | (1 << 4),
        0xE000ED24: (1 << 16) | (1 << 17) | (1 << 18),
        0xE000EDF0: 0,
        0xE000ED90: 1 << 8,
        0xE000ED94: mpu_control,
        0xE000ED98: 5,
        0xE000ED9C: 0x20000000,
        0xE000EDA0: (15 << 1) | (subregion_disable_mask << 8) | (0b011 << 24) | (1 << 28) | 1,
    }
    values.update({0x08000000 + 4 * index: 0x08000101 for index in (2, 3, 4, 5, 6, 11, 14, 15)})
    return Memory(values)


def test_auditor_reuses_mpu_dump_and_restores_selector() -> None:
    """The service retains MPU findings while the shared dump restores RNR."""
    memory = _memory()
    report = CortexMSecurityAuditor(Registers({"msp": 0x20000020, "psp": 0x20000040})).collect(
        memory,
        decode_cpuid(0x410FC240),
    )

    assert memory.writes == [(0xE000ED98, 0), (0xE000ED98, 5)]
    assert any(finding.title == "MPU is enabled" for finding in report.findings)
    assert any(finding.title == "MSP stack region is non-executable" for finding in report.findings)
    assert any(finding.title == "Debug access is disabled" for finding in report.findings)


@pytest.mark.parametrize(
    ("mpu_control", "expected_title"),
    (
        (1, "MSP is not covered by any MPU region"),
        (5, "MSP falls back to the default background map"),
    ),
)
def test_auditor_treats_disabled_mpu_subregions_as_uncovered(
    mpu_control: int,
    expected_title: str,
) -> None:
    """A PMSAv7 SRD bit selects the existing uncovered-stack handling."""
    report = CortexMSecurityAuditor(Registers({"msp": 0x20000020})).collect(
        _memory(mpu_control, subregion_disable_mask=0b00000001),
        decode_cpuid(0x410FC240),
    )

    assert any(finding.title == expected_title for finding in report.findings)
    assert not any(
        finding.title == "MSP stack region is non-executable" for finding in report.findings
    )


def test_auditor_treats_enabled_mpu_subregions_as_covered() -> None:
    """A pointer in an enabled PMSAv7 subregion keeps the non-executable finding."""
    report = CortexMSecurityAuditor(Registers({"msp": 0x20000020})).collect(
        _memory(subregion_disable_mask=0b00000010),
        decode_cpuid(0x410FC240),
    )

    assert any(finding.title == "MSP stack region is non-executable" for finding in report.findings)


def test_auditor_keeps_stm32_rdp_check_in_arm_service(monkeypatch: object) -> None:
    """A recognized STM32 receives its RDP finding from the Arm service."""
    from pyGdbToolkit.arch.arm import security

    memory = _memory()
    memory.values[0x40023C14] = 0x0000AA00
    target = decode_cpuid(0x410FC240)
    device = DeviceReport(
        target,
        object(),  # type: ignore[arg-type]
        "STMicroelectronics",
        FieldValue.known("STM32F4"),
        FieldValue.unavailable("not needed"),
        FieldValue.unavailable("not needed"),
        FieldValue.unavailable("not needed"),
        FieldValue.unavailable("not needed"),
        FieldValue.unavailable("not needed"),
    )
    monkeypatch.setattr(security, "discover_rom_tables", lambda reader: object())
    monkeypatch.setattr(
        security.DEFAULT_PROVIDER_REGISTRY,
        "inspect",
        lambda reader, inspected_target, discovery: device,
    )

    report = CortexMSecurityAuditor(Registers({})).collect(memory, target)

    assert any(finding.title == "RDP level 0 (no protection)" for finding in report.findings)
