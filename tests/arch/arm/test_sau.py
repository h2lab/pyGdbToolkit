# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Tests for Cortex-M Secure Attribution Unit inspection."""

from __future__ import annotations

from pyGdbToolkit.arch.arm.cortex_m import CORTEX_M_CORES, CortexMPart, decode_cpuid
from pyGdbToolkit.arch.arm.sau import (
    SAU_BASE_ADDRESS,
    SauAttribution,
    SauDefaultSecurity,
    SauRegister,
    dump_sau_regions,
    read_sau_status,
)
from pyGdbToolkit.target_memory import TargetReadError, TargetWriteError


class FakeWritableTargetMemory:
    """Configurable SAU fixture with region-selector behavior."""

    def __init__(
        self,
        values: dict[int, int],
        regions: dict[tuple[int, int], int] | None = None,
        failures: set[tuple[int | None, int]] | None = None,
    ) -> None:
        """Initialize fixed registers, selected regions, and read failures."""
        self.values = values
        self.regions = regions or {}
        self.failures = failures or set()
        self.selected = values.get(SAU_BASE_ADDRESS + SauRegister.RNR, 0)
        self.writes: list[tuple[int, int]] = []

    def read_bytes(self, address: int, size: int) -> bytes:
        """Reject byte reads."""
        raise AssertionError((address, size))

    def read_uint16(self, address: int) -> int:
        """Reject halfword reads."""
        raise AssertionError((address, 2))

    def read_uint32(self, address: int) -> int:
        """Read a fixed or selected-region word."""
        if (self.selected, address) in self.failures or (None, address) in self.failures:
            raise TargetReadError(address, 4, "access denied")
        if (self.selected, address) in self.regions:
            return self.regions[(self.selected, address)]
        if address not in self.values:
            raise TargetReadError(address, 4, "not mapped")
        return self.values[address]

    def write_uint32(self, address: int, value: int) -> None:
        """Select an SAU region."""
        self.writes.append((address, value))
        if address != SAU_BASE_ADDRESS + SauRegister.RNR:
            raise TargetWriteError(address, 4, "unsupported write")
        self.selected = value


def _target(part: CortexMPart) -> object:
    """Build a known Cortex-M target."""
    return decode_cpuid(0x410F0000 | (part.value << 4))


def test_catalog_only_describes_sau_for_armv8_m_cores() -> None:
    """Only Armv8-M and Armv8.1-M catalog entries expose the SAU interface."""
    assert CORTEX_M_CORES[CortexMPart.M4].sau is None
    assert CORTEX_M_CORES[CortexMPart.M23].sau is not None
    assert CORTEX_M_CORES[CortexMPart.M85].sau is not None


def test_reads_sau_status_and_default_security() -> None:
    """SAU control bits decode enable, ALLNS, and default attribution."""
    reader = FakeWritableTargetMemory(
        {
            SAU_BASE_ADDRESS + SauRegister.TYPE: 3,
            SAU_BASE_ADDRESS + SauRegister.CTRL: 0b11,
        }
    )

    status = read_sau_status(reader, _target(CortexMPart.M33))  # type: ignore[arg-type]

    assert status.is_available
    assert status.region_count == 3
    assert status.enabled
    assert status.all_non_secure
    assert status.default_security is SauDefaultSecurity.NON_SECURE


def test_unsupported_core_does_not_probe_sau() -> None:
    """Cores without an SAU description stop before target access."""
    status = read_sau_status(FakeWritableTargetMemory({}), _target(CortexMPart.M4))  # type: ignore[arg-type]

    assert not status.is_available


def test_zero_region_sau_does_not_write_selector() -> None:
    """Runtime SAU absence returns status without changing SAU_RNR."""
    reader = FakeWritableTargetMemory(
        {
            SAU_BASE_ADDRESS + SauRegister.TYPE: 0,
            SAU_BASE_ADDRESS + SauRegister.CTRL: 0,
        }
    )

    dump = dump_sau_regions(reader, _target(CortexMPart.M23))  # type: ignore[arg-type]

    assert dump.status.is_available
    assert dump.regions == ()
    assert reader.writes == []


def test_dumps_non_secure_callable_region_and_restores_selector() -> None:
    """SAU regions decode inclusive bounds, NSC attribution, and selector restoration."""
    rnr_address = SAU_BASE_ADDRESS + SauRegister.RNR
    reader = FakeWritableTargetMemory(
        {
            SAU_BASE_ADDRESS + SauRegister.TYPE: 1,
            SAU_BASE_ADDRESS + SauRegister.CTRL: 1,
            rnr_address: 7,
        },
        {
            (0, SAU_BASE_ADDRESS + SauRegister.RBAR): 0x10000000,
            (0, SAU_BASE_ADDRESS + SauRegister.RLAR): 0x10000020 | (1 << 1) | 1,
        },
    )

    dump = dump_sau_regions(reader, _target(CortexMPart.M55))  # type: ignore[arg-type]

    region = dump.regions[0].region
    assert region is not None
    assert region.enabled
    assert region.start_address == 0x10000000
    assert region.end_address == 0x1000003F
    assert region.size_bytes == 64
    assert region.attribution is SauAttribution.NON_SECURE_CALLABLE
    assert reader.writes == [(rnr_address, 0), (rnr_address, 7)]


def test_region_failure_is_retained_and_selector_is_restored() -> None:
    """One unreadable region does not prevent selector restoration."""
    rnr_address = SAU_BASE_ADDRESS + SauRegister.RNR
    reader = FakeWritableTargetMemory(
        {
            SAU_BASE_ADDRESS + SauRegister.TYPE: 1,
            SAU_BASE_ADDRESS + SauRegister.CTRL: 0,
            rnr_address: 2,
        },
        failures={(0, SAU_BASE_ADDRESS + SauRegister.RBAR)},
    )

    dump = dump_sau_regions(reader, _target(CortexMPart.M52))  # type: ignore[arg-type]

    assert not dump.regions[0].is_available
    assert "access denied" in dump.regions[0].unavailable_reason
    assert reader.writes[-1] == (rnr_address, 2)
