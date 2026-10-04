"""Memory discovery adapters and architecture-specific hints."""

import pytest

from pyGdbToolkit.memmap_runtime import (
    DEFAULT_MEMORY_MAP_REGISTRY,
    elf_regions,
    gdb_memory_context,
    parse_server_map,
    remote_memory_map,
    require_stopped_target,
    svd_regions,
)
from pyGdbToolkit.svd import SvdDevice, SvdPeripheral, SvdRegister
from unittest.mock import Mock


def test_remote_chunks():
    """Advance the qXfer offset and join metadata before parsing."""
    chunks = [
        '<memory-map><memory type="ram" ',
        'start="0x20000000" length="0x1000"/></memory-map>',
    ]
    commands = []

    def execute(command):
        commands.append(command)
        index = len(commands) - 1
        return f'received: "{"m" if index == 0 else "l"}{chunks[index]}"'

    regions = remote_memory_map(execute)
    assert regions[0].end == 0x20001000
    assert regions[0].probe_allowed
    assert commands[1].endswith(f"::{len(chunks[0]):x},1000")


@pytest.mark.parametrize(
    "xml",
    [
        "<bad/>",
        '<!DOCTYPE memory-map [<!ENTITY x "bad">]><memory-map/>',
        '<memory-map><memory type="ram" start="-1" length="1"/></memory-map>',
        "<memory-map><memory/></memory-map>",
    ],
)
def test_invalid_xml(xml):
    """Reject invalid ranges and untrusted XML entity definitions."""
    with pytest.raises(ValueError):
        parse_server_map(xml)


def test_unsupported_remote_map():
    """Do not turn unsupported metadata into a fabricated region."""
    with pytest.raises(ValueError, match="does not expose"):
        remote_memory_map(lambda _: 'received: ""')


def test_standard_external_doctype():
    """Accept GDB's standard DTD declaration without fetching external content."""
    assert parse_server_map('<!DOCTYPE memory-map SYSTEM "gdb-memory-map.dtd"><memory-map/>') == []


def test_nonprogressing_remote_map():
    """Stop a server that never advances its metadata transfer."""
    with pytest.raises(ValueError, match="empty continuing"):
        remote_memory_map(lambda _: 'received: "m"')


def test_elf_is_not_a_physical_map():
    """Allocated sections supply candidates, never RAM or Flash certainty."""
    regions = elf_regions(
        "[ 0] 0x1000->0x1100 at 0x100: .text ALLOC LOAD READONLY CODE\n[1] 0x0->0x100 at 0x200: .debug_info READONLY"
    )
    assert len(regions) == 1
    assert regions[0].kind == "unknown"
    assert not regions[0].probe_allowed


def test_svd_envelope():
    """Exclude SVD envelopes from automatic probes even for readable registers."""
    register = SvdRegister("STATUS", None, "", 0x10, 32, "read-only", None, None)
    device = SvdDevice(
        "test",
        None,
        "",
        None,
        8,
        32,
        32,
        0,
        0,
        (SvdPeripheral("UART", "", None, 0x40000000, registers=(register,)),),
    )
    regions = svd_regions(device)
    assert (regions[0].start, regions[0].end) == (0x40000010, 0x40000014)
    assert not regions[0].probe_allowed


def test_architecture_hints_are_not_types():
    """Apply Cortex-M windows only to M-profile, with a portable fallback."""
    registry = DEFAULT_MEMORY_MAP_REGISTRY
    assert (
        registry.provider("armv8-m.main").describe(0x20000000, 0x20001000) == "Cortex-M SRAM space"
    )
    for architecture in ("aarch64", "riscv:rv32", "armv7-a", "arm"):
        assert (
            registry.provider(architecture).describe(0x20000000, 0x20001000)
            == "unknown address space"
        )


def test_display_and_binary_escapes():
    """Decode both GDB display escapes and escaped RSP payload bytes."""
    xml = (
        r'<memory-map>\x0a<memory type="ram" start="0x1000" length="0x100"/><property name="hint">}'
        + chr(0x03)
        + r"</property></memory-map>"
    )
    assert remote_memory_map(lambda _: f'received: "l{xml}"')[0].start == 0x1000


def test_gdb_context_and_stopped_check(fake_gdb, monkeypatch):
    """Capture real adapter fields and reject a running inferior thread."""
    inferior = Mock(num=2, pid=42)
    inferior.architecture.return_value.name.return_value = "riscv:rv32"
    inferior.connection = Mock(num=3, details="localhost:3333")
    thread = Mock(global_num=4)
    thread.name = "core0"
    inferior.threads.return_value = [thread]
    fake_gdb._inferior = inferior
    monkeypatch.setattr(fake_gdb, "selected_thread", lambda: thread, raising=False)
    monkeypatch.setattr(fake_gdb, "objfiles", lambda: [], raising=False)
    context = gdb_memory_context()
    assert (context["inferior"], context["thread"], context["connection"]) == (2, 4, 3)
    assert context["architecture"] == "riscv:rv32"
    thread.is_stopped.return_value = False
    with pytest.raises(fake_gdb.GdbError, match="Stop all threads"):
        require_stopped_target()


def test_no_target_context(fake_gdb):
    """Reject discovery without a selected inferior."""
    with pytest.raises(fake_gdb.GdbError):
        gdb_memory_context()
