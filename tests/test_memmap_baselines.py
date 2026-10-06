# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Manufacturer reference windows remain separate from measured memory."""

import pytest
from io import StringIO
from unittest.mock import Mock

from rich.console import Console

from pyGdbToolkit.arch.memmap import MemoryBaseline, MemoryMapRegistry, MemoryRegion
from pyGdbToolkit import cmd_memmap
from pyGdbToolkit.cmd_memmap import MemmapCmd, MemmapSubcommand, MemoryMapSessionState
from pyGdbToolkit.memmap import MemoryMapReport
from pyGdbToolkit.memmap_runtime import DEFAULT_MEMORY_MAP_REGISTRY
from pyGdbToolkit.session import ToolkitSession


def test_catalog_filter_and_aliases():
    """Resolve manufacturer aliases without architecture or target access."""
    baseline = MemoryBaseline(
        "st",
        "STM32F407",
        "Cortex-M4",
        0x08000000,
        0x08100000,
        "flash",
        "RM0090",
        "Representative maximum, not detected size",
        ("STMicroelectronics",),
    )
    registry = MemoryMapRegistry(baselines=(baseline,))
    assert registry.baselines() == (baseline,)
    assert registry.baselines("ST Microelectronics") == (baseline,)
    assert registry.baselines("ST") == (baseline,)
    with pytest.raises(ValueError, match="Unknown.*nxp"):
        registry.baselines("nxp")


def test_baseline_range_validation():
    """Reject empty reference windows just like declared ranges."""
    with pytest.raises(ValueError):
        MemoryBaseline("test", "family", "unknown", 4, 4, "ram", "manual", "example")


def test_default_catalog_has_requested_manufacturers():
    """Keep the requested brands represented by explicit ARM device families."""
    entries = DEFAULT_MEMORY_MAP_REGISTRY.baselines()
    assert {entry.vendor for entry in entries} == {"st", "nxp", "infineon", "xilinx"}
    assert all(
        entry.family and entry.architecture and entry.reference and entry.note for entry in entries
    )
    assert all(not isinstance(entry, MemoryRegion) for entry in entries)


def test_reference_addresses_and_aliases():
    """Preserve representative SRAM banks and distinguish Flash aliases."""
    registry = DEFAULT_MEMORY_MAP_REGISTRY
    st = registry.baselines("STMicroelectronics")
    assert any(entry.start <= 0x1FFF7800 < entry.end and entry.kind == "otp" for entry in st)
    assert {entry.family for entry in st} == {"STM32"}
    assert any(entry.start == 0x24000000 and entry.end >= 0x24400000 for entry in st)
    nxp = registry.baselines("nxp")
    assert any(entry.start == 0x10000000 and entry.end >= 0x10008000 for entry in nxp)
    assert all("1768" not in entry.family for entry in nxp)
    infineon = registry.baselines("infineon")
    assert any(entry.start == 0x10000000 and entry.end >= 0x10010000 for entry in infineon)
    assert all("4500" not in entry.family for entry in infineon)
    assert {0x08000000, 0x0C000000} <= {entry.start for entry in infineon if entry.kind == "flash"}
    assert registry.baselines("AMD/Xilinx") == registry.baselines("xilinx")
    assert any(entry.end == 0x100000000 for entry in registry.baselines("AMD"))
    assert any(entry.start > 0x100000000 for entry in registry.baselines("AMD"))


def test_cli_catalog_without_connection(monkeypatch):
    """Display all brands and a filtered table without consulting target access."""
    output = StringIO()
    monkeypatch.setattr(cmd_memmap, "CONSOLE", Console(file=output, width=180, color_system=None))
    context = Mock(side_effect=AssertionError("reference tables must not access target context"))
    memory = Mock(side_effect=AssertionError("reference tables must not construct a reader"))
    command = MemmapCmd(ToolkitSession(), context=context, memory=memory)
    command.run("bases", [])
    rendered = output.getvalue()
    assert all(vendor in rendered for vendor in ("st", "nxp", "infineon", "xilinx"))
    assert "not verified" in rendered
    assert command.session.state(MemoryMapSessionState).report is None
    output.seek(0)
    output.truncate(0)
    MemmapSubcommand(command, "bases").invoke("STMicroelectronics", False)
    rendered = output.getvalue()
    assert "STM32" in rendered
    assert "0x08000000" in rendered and "0x10000000" in rendered
    assert "LPC1768" not in rendered
    context.assert_not_called()
    memory.assert_not_called()


def test_catalog_does_not_enable_known_probe(fake_gdb):
    """Having a baseline catalog must not turn it into measured or eligible memory."""
    context = {"architecture": "armv7e-m", "thread": 1}
    memory = Mock()
    command = MemmapCmd(
        ToolkitSession(), context=lambda: context, memory=lambda: memory, stopped=Mock()
    )
    state = command.session.state(MemoryMapSessionState)
    state.report = MemoryMapReport("armv7e-m", dict(context))
    command.run("bases", ["st"])
    assert state.report.regions == []
    with pytest.raises(fake_gdb.GdbError, match="No eligible"):
        command.run("probe", ["--known"])
    memory.read_bytes.assert_not_called()


@pytest.mark.parametrize("args", [["unknown-brand"], ["st", "nxp"]])
def test_invalid_catalog_request(fake_gdb, args):
    """Reject unknown brands and ambiguous filters with a GDB error."""
    command = MemmapCmd(ToolkitSession())
    with pytest.raises(fake_gdb.GdbError):
        command.run("bases", args)
