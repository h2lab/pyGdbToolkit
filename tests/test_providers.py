"""Tests for ROM-keyed vendor providers and STM32 signature readers."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pyGdbToolkit.coresight import (
    MCU_ROM_TABLE_ADDRESS,
    CoreSightDiscovery,
    discover_rom_tables,
)
from pyGdbToolkit.cpuid import decode_cpuid
from pyGdbToolkit.models import CPUID, DeviceReport, FieldValue
from pyGdbToolkit.providers import (
    DEFAULT_PROVIDER_REGISTRY,
    ST_JEP106_IDENTITY,
    STM32_PROFILES,
    ProviderRegistry,
    SignatureLayout,
    Stm32Profile,
    Stm32Provider,
)
from pyGdbToolkit.target_memory import TargetMemory, TargetReadError

CPUID_M4 = decode_cpuid(0x413FC241)
_CIDR_OFFSETS = (0xFF0, 0xFF4, 0xFF8, 0xFFC)
_PIDR_OFFSETS = (0xFE0, 0xFE4, 0xFE8, 0xFEC, 0xFD0)
_FORBIDDEN_LEGACY_ADDRESSES = {
    0x40015800,
    0xE0042000,
    0xE0044000,
    0x44024000,
    0x5C001000,
    0x44001000,
    0x46001000,
}


class FakeTargetMemory:
    """A register-memory fake that records exactly which addresses were read."""

    def __init__(
        self,
        uint16: dict[int, int] | None = None,
        uint32: dict[int, int] | None = None,
        inaccessible: set[int] | None = None,
    ) -> None:
        """Initialize configured values and inaccessible target addresses."""
        self.uint16 = uint16 or {}
        self.uint32 = uint32 or {}
        self.inaccessible = inaccessible or set()
        self.calls: list[tuple[int, int]] = []

    def read_bytes(self, address: int, size: int) -> bytes:
        """Reject byte reads that are not used by these providers."""
        raise NotImplementedError

    def read_uint16(self, address: int) -> int:
        """Read a fake 16-bit target register."""
        self.calls.append((address, 2))
        if address in self.inaccessible:
            raise TargetReadError(address, 2, "access denied")
        if address not in self.uint16:
            raise TargetReadError(address, 2, "not mapped")
        return self.uint16[address]

    def read_uint32(self, address: int) -> int:
        """Read a fake 32-bit target register."""
        self.calls.append((address, 4))
        if address in self.inaccessible:
            raise TargetReadError(address, 4, "access denied")
        if address not in self.uint32:
            raise TargetReadError(address, 4, "not mapped")
        return self.uint32[address]


def _write_mcu_rom_root(
    memory: FakeTargetMemory,
    *,
    part_number: int = 0x486,
    bank: int = 0,
    code: int = 0x20,
    jedec_present: bool = True,
) -> None:
    """Build a valid, empty MCU ROM table for a provider fixture."""
    cidr = (0x0D, 0x10, 0x05, 0xB1)
    pidr = (
        part_number & 0xFF,
        ((code & 0x0F) << 4) | (part_number >> 8),
        ((code >> 4) & 0x07) | (0x08 if jedec_present else 0),
        0,
        bank,
    )
    for offset, value in zip(_CIDR_OFFSETS, cidr, strict=True):
        memory.uint32[MCU_ROM_TABLE_ADDRESS + offset] = value
    for offset, value in zip(_PIDR_OFFSETS, pidr, strict=True):
        memory.uint32[MCU_ROM_TABLE_ADDRESS + offset] = value
    memory.uint32[MCU_ROM_TABLE_ADDRESS] = 0


def _memory_for_profile(profile: Stm32Profile) -> FakeTargetMemory:
    """Create valid MCU-ROM identity and readable documented signatures."""
    memory = FakeTargetMemory()
    _write_mcu_rom_root(
        memory,
        part_number=profile.mcu_rom_part_numbers[0],
        bank=profile.jep106.bank,
        code=profile.jep106.code,
    )
    if (
        profile.signature.flash_size_address is not None
        and profile.signature.flash_size_fixed_kib is None
    ):
        memory.uint16[profile.signature.flash_size_address] = _actual_flash_size(profile.signature)
    if profile.signature.package_address is not None:
        memory.uint16[profile.signature.package_address] = 0xBEEF
    if profile.signature.uid_address is not None:
        for offset, value in zip(
            range(0, 12, 4),
            (0x11111111, 0x22222222, 0x33333333),
            strict=True,
        ):
            memory.uint32[profile.signature.uid_address + offset] = value
    return memory


def _actual_flash_size(signature: SignatureLayout) -> int:
    """Return a valid non-sentinel factory flash-size value for a test target."""
    if signature.flash_size_mask == 0x007F:
        return 64
    return 256


def test_stm32_profile_is_selected_from_mcu_rom_identity_only() -> None:
    """A full JEP106 plus part match selects the known profile and signatures."""
    profile = STM32_PROFILES[0]
    memory = _memory_for_profile(profile)
    discovery = discover_rom_tables(memory)

    report = DEFAULT_PROVIDER_REGISTRY.inspect(memory, CPUID_M4, discovery)

    assert report.vendor == "STMicroelectronics"
    assert report.product_line.value == profile.product_line
    assert report.part_number.value == (
        "STM32C01xx (exact ordering code unavailable from MCU-ROM part)"
    )
    assert report.ram.value == "6 KiB"
    assert report.serial_number.value == "0x111111112222222233333333 (96-bit UID)"
    assert all(address not in _FORBIDDEN_LEGACY_ADDRESSES for address, _ in memory.calls)


@pytest.mark.parametrize("profile", STM32_PROFILES, ids=lambda profile: profile.product_line)
def test_each_stm32_profile_is_selected_from_its_mcu_rom_part(
    profile: Stm32Profile,
) -> None:
    """Every retained STM32 profile is selected from JEP106 and MCU-ROM part data."""
    memory = _memory_for_profile(profile)
    discovery = discover_rom_tables(memory)

    report = DEFAULT_PROVIDER_REGISTRY.inspect(memory, CPUID_M4, discovery)

    assert report.vendor == "STMicroelectronics"
    assert report.product_line.value == profile.product_line
    assert all(address not in _FORBIDDEN_LEGACY_ADDRESSES for address, _ in memory.calls)


def test_signature_read_failure_is_field_specific_after_rom_profile_match() -> None:
    """An inaccessible signature register does not hide ROM-selected metadata."""
    profile = STM32_PROFILES[0]
    memory = _memory_for_profile(profile)
    assert profile.signature.uid_address is not None
    memory.inaccessible.add(profile.signature.uid_address)
    discovery = discover_rom_tables(memory)

    report = Stm32Provider().inspect(memory, CPUID_M4, discovery)

    assert report is not None
    assert report.product_line.value == profile.product_line
    assert "could not read documented 96-bit UID" in report.serial_number.unavailable_reason


def test_unknown_mcu_rom_part_strictly_uses_generic_fallback() -> None:
    """An ST identity without a proven part mapping cannot select legacy metadata."""
    memory = FakeTargetMemory()
    _write_mcu_rom_root(memory, part_number=0x487)
    discovery = discover_rom_tables(memory)

    report = DEFAULT_PROVIDER_REGISTRY.inspect(memory, CPUID_M4, discovery)

    assert report.vendor == "Generic Cortex-M"
    assert report.product_line.value is None
    assert "bank 0, code 0x20, part 0x487" in report.product_line.unavailable_reason
    assert all(address not in _FORBIDDEN_LEGACY_ADDRESSES for address, _ in memory.calls)


def test_unknown_manufacturer_strictly_uses_generic_fallback() -> None:
    """A valid non-ST ROM identity does not read any vendor signatures."""
    memory = FakeTargetMemory()
    _write_mcu_rom_root(memory, code=0x21)
    discovery = discover_rom_tables(memory)

    report = DEFAULT_PROVIDER_REGISTRY.inspect(memory, CPUID_M4, discovery)

    assert report.vendor == "Generic Cortex-M"
    assert "code 0x21" in report.product_line.unavailable_reason
    assert all(address not in _FORBIDDEN_LEGACY_ADDRESSES for address, _ in memory.calls)


def test_unavailable_mcu_rom_strictly_uses_generic_fallback() -> None:
    """No fallback reads occur when the MCU discovery root is inaccessible."""
    memory = FakeTargetMemory()
    discovery = discover_rom_tables(memory)

    report = DEFAULT_PROVIDER_REGISTRY.inspect(memory, CPUID_M4, discovery)

    assert report.vendor == "Generic Cortex-M"
    assert "MCU-ROM identity unavailable" in report.product_line.unavailable_reason
    assert all(address not in _FORBIDDEN_LEGACY_ADDRESSES for address, _ in memory.calls)


def test_profile_reads_only_its_documented_signature_layout() -> None:
    """A ROM match reads no metadata other than the selected profile's layout."""
    signature = SignatureLayout(
        flash_size_address=0x1FFF75E0,
        uid_address=0x1FFF7590,
        package_address=0x1FFF7500,
    )
    profile = Stm32Profile("STM32 test line", ST_JEP106_IDENTITY, (0x123,), signature, 32)
    memory = _memory_for_profile(profile)
    discovery = discover_rom_tables(memory)

    report = Stm32Provider((profile,)).inspect(memory, CPUID_M4, discovery)

    assert report is not None
    assert report.flash.value == "256 KiB"
    assert report.package.value == "0xBEEF (raw package code; package type mapping unavailable)"
    assert report.serial_number.value == "0x111111112222222233333333 (96-bit UID)"
    signature_calls = memory.calls[-5:]
    assert signature_calls == [
        (0x1FFF75E0, 2),
        (0x1FFF7500, 2),
        (0x1FFF7590, 4),
        (0x1FFF7594, 4),
        (0x1FFF7598, 4),
    ]


def test_ambiguous_rom_mapping_does_not_read_signatures() -> None:
    """Profiles sharing a full ROM key report ambiguity instead of guessing."""
    signature = SignatureLayout(flash_size_address=0x1FFF75E0, uid_address=0x1FFF7590)
    profiles = (
        Stm32Profile("STM32 test line A", ST_JEP106_IDENTITY, (0xAAA,), signature),
        Stm32Profile("STM32 test line B", ST_JEP106_IDENTITY, (0xAAA,), signature),
    )
    memory = FakeTargetMemory()
    _write_mcu_rom_root(memory, part_number=0xAAA)
    discovery = discover_rom_tables(memory)
    call_count_before_provider = len(memory.calls)

    report = Stm32Provider(profiles).inspect(memory, CPUID_M4, discovery)

    assert report is not None
    assert report.product_line.value == "Ambiguous: STM32 test line A, STM32 test line B"
    assert report.flash.value is None
    assert len(memory.calls) == call_count_before_provider


@dataclass
class FixedProvider:
    """A provider that returns a predetermined report for registry ordering."""

    report: DeviceReport | None

    def inspect(
        self,
        reader: TargetMemory,
        cpuid: CPUID,
        discovery: CoreSightDiscovery,
    ) -> DeviceReport | None:
        """Return the configured report."""
        del reader, cpuid, discovery
        return self.report


def test_provider_registry_respects_provider_order() -> None:
    """The first matching provider wins."""
    memory = FakeTargetMemory()
    _write_mcu_rom_root(memory)
    discovery = discover_rom_tables(memory)
    unavailable = FieldValue.unavailable("not selected")
    expected = DeviceReport(
        cpuid=CPUID_M4,
        discovery=discovery,
        vendor="First",
        product_line=unavailable,
        part_number=unavailable,
        ram=unavailable,
        flash=unavailable,
        package=unavailable,
        serial_number=unavailable,
    )
    registry = ProviderRegistry((FixedProvider(expected), FixedProvider(None)))

    assert registry.inspect(memory, CPUID_M4, discovery) == expected
