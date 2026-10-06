"""Tests for CoreSight identity decoding and bounded ROM-table traversal."""

# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest

from pyGdbToolkit.arch.arm.coresight import (
    MCU_ROM_TABLE_ADDRESS,
    PROCESSOR_ROM_TABLE_ADDRESS,
    ROM_TABLE_COMPONENT_CLASS,
    ComponentIdentityError,
    RomTableError,
    decode_component_id,
    decode_peripheral_id,
    discover_rom_tables,
    walk_rom_table,
)
from pyGdbToolkit.target_memory import TargetReadError
from pyGdbToolkit.arch.arm.trace import discover_trace_capabilities
from pyGdbToolkit.arch.arm.trace import probe_trace_capabilities
from pyGdbToolkit.arch.arm.cortex_m import decode_cpuid
from pyGdbToolkit.arch.trace import TraceComponentKind

_CIDR_OFFSETS = (0xFF0, 0xFF4, 0xFF8, 0xFFC)
_PIDR_OFFSETS = (0xFE0, 0xFE4, 0xFE8, 0xFEC, 0xFD0)


class FakeTargetMemory:
    """A 32-bit target-memory fake with precise fault reporting."""

    def __init__(self, uint32: dict[int, int] | None = None) -> None:
        """Initialize readable 32-bit words."""
        self.uint32 = uint32 or {}
        self.calls: list[tuple[int, int]] = []

    def read_bytes(self, address: int, size: int) -> bytes:
        """Reject byte reads that CoreSight discovery does not use."""
        raise NotImplementedError

    def read_uint16(self, address: int) -> int:
        """Reject 16-bit reads that CoreSight discovery does not use."""
        raise NotImplementedError

    def read_uint32(self, address: int) -> int:
        """Read one configured word or report a target fault."""
        self.calls.append((address, 4))
        if address not in self.uint32:
            raise TargetReadError(address, 4, "not mapped")
        return self.uint32[address]


def _cidr(component_class: int) -> tuple[int, int, int, int]:
    """Encode valid CoreSight CIDR bytes for a test component."""
    return (0x0D, component_class << 4, 0x05, 0xB1)


def _pidr(
    part_number: int,
    *,
    bank: int = 0,
    code: int = 0x20,
    jedec_present: bool = True,
) -> tuple[int, int, int, int, int]:
    """Encode standard CoreSight PIDR bytes for test fixtures only."""
    assert 0 <= part_number <= 0xFFF
    assert 0 <= bank <= 0xF
    assert 0 <= code <= 0x7F
    return (
        part_number & 0xFF,
        ((code & 0x0F) << 4) | (part_number >> 8),
        ((code >> 4) & 0x07) | (0x08 if jedec_present else 0),
        0,
        bank,
    )


def _write_component(
    memory: FakeTargetMemory,
    base: int,
    *,
    component_class: int,
    part_number: int = 0,
    bank: int = 0,
    code: int = 0x20,
    jedec_present: bool = True,
) -> None:
    """Populate CIDR and PIDR words for one fake component."""
    for offset, value in zip(_CIDR_OFFSETS, _cidr(component_class), strict=True):
        memory.uint32[base + offset] = value
    for offset, value in zip(
        _PIDR_OFFSETS,
        _pidr(part_number, bank=bank, code=code, jedec_present=jedec_present),
        strict=True,
    ):
        memory.uint32[base + offset] = value


def test_detects_trace_components_in_nested_rom_table() -> None:
    """Trace discovery follows nested entries and reads only capability registers."""
    memory = FakeTargetMemory()
    root = MCU_ROM_TABLE_ADDRESS
    nested = root - 0x1000
    _write_component(memory, root, component_class=1)
    _write_component(memory, nested, component_class=1)
    memory.uint32[root] = 0xFFFFF003
    memory.uint32[root + 4] = 0
    for index, part in enumerate((0xD21, 0x907, 0x932)):
        base = nested - (index + 1) * 0x1000
        _write_component(memory, base, component_class=9, part_number=part, bank=4, code=0x3B)
        memory.uint32[nested + index * 4] = ((base - nested) & 0xFFFFF000) | 3
    memory.uint32[nested + 12] = 0
    etm_base = nested - 0x1000
    memory.uint32[etm_base + 0xFBC] = 0x47734A13
    memory.uint32[etm_base + 0x1EC] = 0x00330000
    memory.uint32[nested - 0x2000 + 4] = 2048
    capabilities = discover_trace_capabilities(memory)

    assert capabilities.is_available
    assert capabilities.has_etm() and capabilities.has_etb() and capabilities.has_mtb()
    assert not capabilities.has_etf()
    etm, etb, mtb = capabilities.components
    assert etm.version == "4.3"
    assert etm.security_filtering is True
    assert etm.secure_exception_levels == 3
    assert etm.nonsecure_exception_levels == 3
    assert etb.buffer_size_bytes == 8192
    assert mtb.kind == TraceComponentKind.MTB
    assert (etm_base + 0x1E4, 4) not in memory.calls


@pytest.mark.parametrize("security", (False, True))
def test_legacy_etm_version_and_security(security: bool) -> None:
    """ETMIDR uses the legacy architecture encoding, not PIDR revision."""
    memory = FakeTargetMemory()
    root = MCU_ROM_TABLE_ADDRESS
    base = root - 0x1000
    _write_component(memory, root, component_class=1)
    _write_component(memory, base, component_class=9, part_number=0x925, bank=4, code=0x3B)
    memory.uint32[root] = 0xFFFFF003
    memory.uint32[root + 4] = 0
    memory.uint32[base + 0x1E4] = 0x240 | (int(security) << 12)

    capabilities = discover_trace_capabilities(memory)

    assert capabilities.has_etm()
    assert capabilities.components[0].version == "3.4"
    assert capabilities.components[0].security_filtering is security


def test_trace_optional_faults_do_not_erase_presence() -> None:
    """Missing capability registers remain unknown without disabling discovery."""
    memory = FakeTargetMemory()
    root = MCU_ROM_TABLE_ADDRESS
    base = root - 0x1000
    _write_component(memory, root, component_class=1)
    _write_component(memory, base, component_class=9, part_number=0x907, bank=4, code=0x3B)
    memory.uint32[root] = 0xFFFFF003
    memory.uint32[root + 4] = 0

    capabilities = discover_trace_capabilities(memory)

    assert capabilities.has_etb()
    assert capabilities.components[0].buffer_size_bytes is None
    assert capabilities.components[0].registers[-1].unavailable_reason is not None


def test_trace_probe_accepts_real_cortex_m_description() -> None:
    """The registered backend accepts the actual decoded processor model."""
    memory = FakeTargetMemory()
    _write_component(memory, MCU_ROM_TABLE_ADDRESS, component_class=1)
    memory.uint32[MCU_ROM_TABLE_ADDRESS] = 0
    result = probe_trace_capabilities(memory, decode_cpuid(0x410FC241))
    assert result.is_available
    assert result.components == ()


@pytest.mark.parametrize(
    ("part", "bank", "code", "devarch", "expected"),
    (
        (0x907, 0, 0x20, 0, None),
        (0x907, 4, 0x3B, 0x47701A14, None),
        (0x123, 0, 0x20, 0x47700A31, TraceComponentKind.MTB),
        (0x925, 4, 0x3B, 0x12304A13, None),
        (0x932, 4, 0x3B, 0, TraceComponentKind.MTB),
    ),
)
def test_trace_identity_prevents_false_positives(part, bank, code, devarch, expected):
    """DEVARCH owns identification; PIDR fallbacks require the full Arm designer."""
    memory = FakeTargetMemory()
    root = MCU_ROM_TABLE_ADDRESS
    base = root - 0x1000
    _write_component(memory, root, component_class=1)
    _write_component(memory, base, component_class=9, part_number=part, bank=bank, code=code)
    memory.uint32[root] = 0xFFFFF003
    memory.uint32[root + 4] = 0
    memory.uint32[base + 0xFBC] = devarch
    result = discover_trace_capabilities(memory)
    assert [component.kind for component in result.components] == (
        [] if expected is None else [expected]
    )


@pytest.mark.parametrize("idr3, security", ((0x00300000, False), (0x00330000, True), (None, None)))
def test_etm4_security_is_false_or_unknown_as_advertised(idr3, security):
    """An unreadable IDR3 is not proof of absent Secure/non-Secure filtering."""
    memory = FakeTargetMemory()
    root = MCU_ROM_TABLE_ADDRESS
    base = root - 0x1000
    _write_component(memory, root, component_class=1)
    _write_component(memory, base, component_class=9)
    memory.uint32[root] = 0xFFFFF003
    memory.uint32[root + 4] = 0
    memory.uint32[base + 0xFBC] = 0x47704A13
    if idr3 is not None:
        memory.uint32[base + 0x1EC] = idr3
    result = discover_trace_capabilities(memory)
    assert result.has_etm()
    assert result.components[0].version == "4.0"
    assert result.components[0].security_filtering is security


@pytest.mark.parametrize("configuration", (0, 1, 2, 3))
def test_tmc_configuration_distinguishes_embedded_from_external_buffers(configuration):
    """TMC ETR/ETS are not ETBs; RSZ is read only for embedded buffers."""
    memory = FakeTargetMemory()
    root = MCU_ROM_TABLE_ADDRESS
    base = root - 0x1000
    _write_component(memory, root, component_class=1)
    _write_component(memory, base, component_class=9, part_number=0x961, bank=4, code=0x3B)
    memory.uint32[root] = 0xFFFFF003
    memory.uint32[root + 4] = 0
    memory.uint32[base + 0xFC8] = configuration << 6
    memory.uint32[base + 4] = 1024
    result = discover_trace_capabilities(memory)
    assert result.has_etb() is (configuration == 0)
    assert result.has_etf() is (configuration == 2)
    if configuration in (0, 2):
        assert result.components[0].buffer_size_bytes == 4096
    else:
        assert (base + 4, 4) not in memory.calls


def test_unreadable_rom_is_not_reported_as_confirmed_absence():
    """An inaccessible topology retains its reason and cannot establish absence."""
    result = discover_trace_capabilities(FakeTargetMemory())
    assert not result.is_available
    assert result.unavailable_reason
    assert not result.has_etm()


@pytest.mark.parametrize("idr", (None, 0, 0x410))
def test_etm4_part_fallback_keeps_unknown_versions_unknown(idr):
    """An identified ETM stays present when its optional version register is unreadable."""
    memory = FakeTargetMemory()
    root = MCU_ROM_TABLE_ADDRESS
    base = root - 0x1000
    _write_component(memory, root, component_class=1)
    _write_component(memory, base, component_class=9, part_number=0x975, bank=4, code=0x3B)
    memory.uint32[root] = 0xFFFFF003
    memory.uint32[root + 4] = 0
    if idr is not None:
        memory.uint32[base + 0x1E4] = idr
    result = discover_trace_capabilities(memory)
    assert result.has_etm()
    assert result.components[0].version == ("4.1" if idr == 0x410 else None)
    assert result.components[0].security_filtering is None


def test_generic_class_components_are_not_probed_for_trace_registers():
    """CoreSight-only registers are never read on generic-class peripherals."""
    memory = FakeTargetMemory()
    root = MCU_ROM_TABLE_ADDRESS
    base = root - 0x1000
    _write_component(memory, root, component_class=1)
    _write_component(memory, base, component_class=0xE, part_number=0x925, bank=4, code=0x3B)
    memory.uint32[root] = 0xFFFFF003
    memory.uint32[root + 4] = 0
    result = discover_trace_capabilities(memory)
    assert result.components == ()
    assert (base + 0xFBC, 4) not in memory.calls


def test_decodes_st_jep106_identity_and_component_part() -> None:
    """PIDR preserves ST's full JEP106 identity and a 12-bit component part."""
    peripheral_id = decode_peripheral_id(_pidr(0x486, bank=0, code=0x20))

    assert peripheral_id.part_number == 0x486
    assert peripheral_id.jedec_present
    assert peripheral_id.jep106 is not None
    assert peripheral_id.jep106.bank == 0
    assert peripheral_id.jep106.code == 0x20


def test_rejects_invalid_cidr_preamble() -> None:
    """PIDR cannot be trusted when the component CIDR is not standard."""
    with pytest.raises(ComponentIdentityError, match="standard CoreSight component preamble"):
        decode_component_id((0x00, 0x10, 0x05, 0xB1))


def test_decodes_standard_packed_cidr_class() -> None:
    """CIDR1's high nibble is the component class in packed little-endian CIDR."""
    component_id = decode_component_id((0x0D, 0x10, 0x05, 0xB1))

    assert component_id.raw == (0x0D, 0x10, 0x05, 0xB1)
    assert component_id.component_class == ROM_TABLE_COMPONENT_CLASS


def test_jedec_absence_falls_back_to_processor_rom_root() -> None:
    """An invalid MCU root falls back to the standard processor ROM root."""
    peripheral_id = decode_peripheral_id(_pidr(0x486, jedec_present=False))
    assert not peripheral_id.jedec_present
    assert peripheral_id.jep106 is None

    memory = FakeTargetMemory()
    _write_component(
        memory,
        MCU_ROM_TABLE_ADDRESS,
        component_class=1,
        part_number=0x486,
        jedec_present=False,
    )
    memory.uint32[MCU_ROM_TABLE_ADDRESS] = 0
    _write_component(
        memory,
        PROCESSOR_ROM_TABLE_ADDRESS,
        component_class=1,
        part_number=0x486,
    )
    memory.uint32[PROCESSOR_ROM_TABLE_ADDRESS] = 0

    discovery = discover_rom_tables(memory)

    assert discovery.mcu_rom.is_available
    assert discovery.mcu_rom.base == PROCESSOR_ROM_TABLE_ADDRESS


def test_valid_mcu_root_prevents_a_second_root_probe() -> None:
    """A valid first root owns traversal, including its nested components."""
    memory = FakeTargetMemory()
    _write_component(memory, MCU_ROM_TABLE_ADDRESS, component_class=1, part_number=0x486)
    memory.uint32[MCU_ROM_TABLE_ADDRESS] = 0

    discovery = discover_rom_tables(memory)

    assert discovery.mcu_rom.is_available
    assert discovery.mcu_rom.base == MCU_ROM_TABLE_ADDRESS
    assert all(
        not (PROCESSOR_ROM_TABLE_ADDRESS <= address < PROCESSOR_ROM_TABLE_ADDRESS + 0x1000)
        for address, _ in memory.calls
    )


def test_walks_positive_and_negative_signed_format_one_offsets() -> None:
    """Format-1 entries resolve signed 4-KiB offsets relative to each table."""
    positive_base = 0x20000000
    positive_memory = FakeTargetMemory()
    _write_component(positive_memory, positive_base, component_class=1)
    _write_component(positive_memory, positive_base + 0x1000, component_class=9)
    positive_memory.uint32[positive_base] = 0x00001003
    positive_memory.uint32[positive_base + 4] = 0

    positive_table = walk_rom_table(positive_memory, positive_base)

    assert positive_table.entries[0].component_base == positive_base + 0x1000
    assert positive_table.components[0].component_id.component_class == 9

    negative_base = 0x20001000
    negative_memory = FakeTargetMemory()
    _write_component(negative_memory, negative_base, component_class=1)
    _write_component(negative_memory, negative_base - 0x1000, component_class=9)
    negative_memory.uint32[negative_base] = 0xFFFFF003
    negative_memory.uint32[negative_base + 4] = 0

    negative_table = walk_rom_table(negative_memory, negative_base)

    assert negative_table.entries[0].component_base == negative_base - 0x1000


@pytest.mark.parametrize(
    ("entry", "message"),
    ((0x00001001, "does not use format 1"),),
)
def test_rejects_invalid_rom_entry_presence_or_format(entry: int, message: str) -> None:
    """Nonzero entries must be present format-1 entries."""
    base = 0x20000000
    memory = FakeTargetMemory()
    _write_component(memory, base, component_class=1)
    memory.uint32[base] = entry

    with pytest.raises(RomTableError, match=message):
        walk_rom_table(memory, base)


def test_skips_disabled_rom_entries() -> None:
    """A nonzero entry without PRESENT set is skipped before format validation."""
    base = 0x20000000
    memory = FakeTargetMemory()
    _write_component(memory, base, component_class=1)
    memory.uint32[base] = 0x00001000
    memory.uint32[base + 4] = 0

    table = walk_rom_table(memory, base)

    assert table.entries == ()
    assert table.components == ()


def test_zero_terminator_ends_table_without_reading_following_entries() -> None:
    """A zero entry is a terminator rather than an invalid nonpresent entry."""
    base = 0x20000000
    memory = FakeTargetMemory()
    _write_component(memory, base, component_class=1)
    memory.uint32[base] = 0

    table = walk_rom_table(memory, base)

    assert table.entries == ()
    assert (base + 4, 4) not in memory.calls


def test_reports_target_faults_while_reading_table_entries() -> None:
    """A target fault becomes a contextual ROM-table error."""
    base = 0x20000000
    memory = FakeTargetMemory()
    _write_component(memory, base, component_class=1)

    with pytest.raises(RomTableError, match="could not read ROM-table entry 0"):
        walk_rom_table(memory, base)


def test_bounds_unterminated_tables_and_blocks_repeated_or_nested_cycles() -> None:
    """The walker requires termination and cannot loop through aliases or nesting."""
    base = 0x20000000
    bounded_memory = FakeTargetMemory()
    _write_component(bounded_memory, base, component_class=1)
    _write_component(bounded_memory, base + 0x1000, component_class=9)
    bounded_memory.uint32[base] = 0x00001003

    with pytest.raises(RomTableError, match="exceeded 1 entries"):
        walk_rom_table(bounded_memory, base, max_entries=1)

    cyclic_memory = FakeTargetMemory()
    _write_component(cyclic_memory, base, component_class=1)
    cyclic_memory.uint32[base] = 0x00000003

    with pytest.raises(RomTableError, match="repeats component"):
        walk_rom_table(cyclic_memory, base)

    nested_memory = FakeTargetMemory()
    _write_component(nested_memory, base, component_class=1)
    _write_component(nested_memory, base + 0x1000, component_class=1)
    nested_memory.uint32[base] = 0x00001003
    nested_memory.uint32[base + 4] = 0

    with pytest.raises(RomTableError, match="exceeded nesting limit 0"):
        walk_rom_table(nested_memory, base, max_nesting=0)
