"""CLI memory-map safety, context and export contracts."""

import json
from unittest.mock import Mock

import pytest

from pyGdbToolkit.arch.memmap import ExecutionHint, MemoryBaseline, MemoryRegion, TargetFingerprint
from pyGdbToolkit.cmd_memmap import MemmapCmd, MemmapSubcommand, MemoryMapSessionState
from pyGdbToolkit.cmd_svd import SvdSessionState
from pyGdbToolkit.memmap import MemoryMapReport
from pyGdbToolkit.session import ToolkitSession
from pyGdbToolkit.target_memory import TargetReadError


@pytest.fixture
def command():
    """Use adapters that make any unexpected memory access observable."""
    memory = Mock()
    memory.read_bytes.return_value = bytes(4)
    context = {"architecture": "riscv:rv32", "inferior": 1, "thread": 1}
    parent = MemmapCmd(
        ToolkitSession(), context=lambda: dict(context), memory=lambda: memory, stopped=Mock()
    )
    parent.session.state(MemoryMapSessionState).report = MemoryMapReport(
        "riscv:rv32",
        dict(context),
        [MemoryRegion(0x1000, 0x1010, "ram", "gdb-server", probe_allowed=True)],
    )
    return parent, memory, context


def test_known_probe(command):
    """Probe the current view without writes or AP selection."""
    parent, memory, _ = command
    parent.run("probe", ["--known", "--stride", "4", "--max-reads", "2"])
    assert memory.read_bytes.call_count == 2
    parent.stopped.assert_called_once()
    memory.write_uint32.assert_not_called()


def test_show_brief_renders_one_table_and_preserves_evidence(command, monkeypatch):
    """Short rendering keeps the complete report and does not access target memory."""
    from pyGdbToolkit import cmd_memmap
    from rich.table import Table

    parent, memory, _ = command
    report = parent._report()
    report.notes.append("Detailed analysis retained")
    report.execution_hints = [ExecutionHint("PC", 0x18003514, "code", "Current PC")]
    report.candidates = [
        MemoryBaseline(
            "st", "STM32", "Cortex-M", 0x18000000, 0x19000000, "rom-or-flash", "maps", "candidate"
        ),
        MemoryBaseline(
            "st", "STM32", "Cortex-M", 0x08000000, 0x09000000, "flash", "maps", "unconfirmed"
        ),
    ]
    original = report.to_dict()
    console = Mock()
    monkeypatch.setattr(cmd_memmap, "CONSOLE", console)
    parent.run("show", ["--brief"])
    console.print.assert_called_once()
    table = console.print.call_args.args[0]
    assert isinstance(table, Table)
    assert len(table.rows) == 2
    assert table.columns[0]._cells == ["0x1000", "0x18000000"]
    assert table.columns[3]._cells[1] == "candidate: PC"
    assert report.to_dict() == original
    memory.read_bytes.assert_not_called()


@pytest.mark.parametrize(
    "operation,args",
    [
        ("discover", ["--brief"]),
        ("probe", ["--known", "--brief"]),
        ("show", ["--unknown"]),
        ("show", ["unexpected"]),
    ],
)
def test_brief_is_only_a_show_option(command, fake_gdb, operation, args):
    """Other commands retain their syntax and reject this display-only option."""
    parent, memory, _ = command
    with pytest.raises(fake_gdb.GdbError):
        parent.run(operation, args)
    memory.read_bytes.assert_not_called()


def test_read_error_is_retained(command):
    """Keep exact error evidence and unknown protection in the report."""
    parent, memory, _ = command
    memory.read_bytes.side_effect = TargetReadError(0x1000, 4, "fault")
    parent.run("probe", ["--known"])
    report = parent._report()
    assert report.observations[0].status == "read-error"
    assert report.regions[0].protection == "unknown"


def test_core_change_refuses_probe(command, fake_gdb):
    """Never reuse a declaration under a different access context."""
    parent, memory, context = command
    context["thread"] = 2
    with pytest.raises(fake_gdb.GdbError, match="context changed"):
        parent.run("probe", ["--known"])
    memory.read_bytes.assert_not_called()


def test_running_target_refuses_probe(command, fake_gdb):
    """A running target must not receive even the first probe read."""
    parent, memory, _ = command
    parent.stopped.side_effect = fake_gdb.error("target running")
    with pytest.raises(fake_gdb.GdbError, match="target running"):
        parent.run("probe", ["--known"])
    memory.read_bytes.assert_not_called()


def test_svd_replacement_refuses_probe(command, fake_gdb):
    """Do not probe using exclusions from an older SVD snapshot."""
    parent, memory, _ = command
    parent.session.state(SvdSessionState).device = Mock()
    with pytest.raises(fake_gdb.GdbError, match="SVD context changed"):
        parent.run("probe", ["--known"])
    memory.read_bytes.assert_not_called()


def test_svd_overlap_excludes_entire_candidate(command, fake_gdb):
    """A server RAM declaration cannot override a conflicting MMIO description."""
    parent, memory, _ = command
    parent._report().regions.append(MemoryRegion(0x1004, 0x1008, "registers", "svd"))
    with pytest.raises(fake_gdb.GdbError, match="No eligible"):
        parent.run("probe", ["--known"])
    memory.read_bytes.assert_not_called()


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--known", "--range", "0:4"],
        ["--range", "0:4"],
        ["--range", "1:4", "--allow-unsafe"],
        ["--known", "--stride", "0"],
        ["--known", "--max-reads", "4097"],
        ["--known", "--timeout", "nan"],
        ["--ap", "0"],
        ["--help"],
        ["--known", "--allow-unsafe"],
    ],
)
def test_invalid_probe_never_reads(command, fake_gdb, args):
    """Reject unsafe defaults, invalid budgets and unsupported AP routing."""
    parent, memory, _ = command
    with pytest.raises(fake_gdb.GdbError):
        parent.run("probe", args)
    memory.read_bytes.assert_not_called()


def test_explicit_range(command):
    """Require explicit acceptance and preserve the requested range in run metadata."""
    parent, memory, _ = command
    parent.run("probe", ["--range", "0x40000000:0x40000008", "--allow-unsafe", "--stride", "4"])
    assert memory.read_bytes.call_count == 2
    assert parent._report().probe_runs[0]["ranges"] == [{"start": 0x40000000, "end": 0x40000008}]


def test_export_and_reset(command, tmp_path, fake_gdb):
    """Export metadata without target reads and clear it on session reset."""
    parent, memory, _ = command
    path = tmp_path / "memory map.json"
    MemmapSubcommand(parent, "report").invoke(f'"{path}"', False)
    data = json.loads(path.read_text())
    assert data["context"]["thread"] == 1
    assert data["regions"][0]["protection"] == "unknown"
    memory.read_bytes.assert_not_called()
    parent.session.reset()
    with pytest.raises(fake_gdb.GdbError, match="discover"):
        parent.run("show", [])


def test_discover_partial_sources(command, fake_gdb, monkeypatch):
    """Unsupported server/DAP discovery leaves ELF hints usable without probing."""
    parent, memory, _ = command
    parent.dap = Mock()
    parent.dap.collect_report.side_effect = fake_gdb.error("unsupported DAP")

    def execute(command, to_string):
        if command.startswith("maintenance packet"):
            return 'received: ""'
        return "[0] 0x1000->0x2000 at 0x100: .text ALLOC LOAD CODE"

    monkeypatch.setattr(fake_gdb, "execute", execute, raising=False)
    parent.run("discover", [])
    report = parent._report()
    assert [region.source for region in report.regions] == ["elf"]
    assert any("DAP metadata unavailable" in note for note in report.notes)
    memory.read_bytes.assert_not_called()


def test_help_registration(command):
    """Publish the new command through the shared session command catalog."""
    parent, _, _ = command
    assert "memmap" in {help.name for help in parent.session.commands}


def test_discover_publishes_and_renders_identity(command, fake_gdb, monkeypatch, tmp_path):
    """Fill shared session metadata from a confirmed fingerprint without SVD or ELF."""
    parent, memory, _ = command
    parent.fingerprint = lambda architecture, dap: TargetFingerprint(
        "STMicroelectronics",
        "STM32N6 product line",
        architecture,
        "hardware-confirmed",
        ("hardware",),
    )
    xml = '<memory-map><memory type="ram" start="0x24000000" length="0x1000" name="SRAM"/></memory-map>'
    monkeypatch.setattr(
        fake_gdb,
        "execute",
        lambda text, to_string: (
            f'received: "l{xml}"' if text.startswith("maintenance packet") else ""
        ),
        raising=False,
    )
    parent.run("discover", ["--verify"])
    report = parent._report()
    assert parent.session.discovery is report
    assert parent.session.target_info.soc == "STM32N6 product line"
    assert parent.session.memory_regions[0].name == "SRAM"
    assert report.candidates
    assert memory.read_bytes.call_count == 2
    assert memory.read_bytes.call_args_list[1].args == (0x24000FFC, 4)
    path = tmp_path / "report.json"
    parent.run("report", [str(path)])
    exported = json.loads(path.read_text())
    assert exported["fingerprint"]["confidence"] == "hardware-confirmed"
    assert any(candidate["status"] == "declared-overlap" for candidate in exported["candidates"])
    with pytest.raises(fake_gdb.GdbError, match="conflicts"):
        parent.run("discover", ["--vendor", "nxp"])
    assert parent.session.discovery is None


def test_uncertain_fingerprint_stays_out_of_session(command, fake_gdb, monkeypatch):
    """An incomplete identification remains visible but cannot become shared fact."""
    parent, memory, _ = command
    parent.fingerprint = lambda architecture, dap: TargetFingerprint(
        "STMicroelectronics", "STM32", architecture, "server-reported", ("configuration",)
    )
    monkeypatch.setattr(
        fake_gdb, "execute", lambda *args, **kwargs: 'received: "l<memory-map/>"', raising=False
    )
    parent.run("discover", [])
    assert parent.session.target_info is None
    assert parent._report().fingerprint.confidence == "server-reported"
    memory.read_bytes.assert_not_called()


def test_ignore_map_requires_explicit_consent(command, fake_gdb):
    """The bypass cannot affect ordinary known-region sampling."""
    parent, memory, _ = command
    with pytest.raises(fake_gdb.GdbError, match="requires"):
        parent.run("probe", ["--known", "--ignore-memory-map"])
    memory.read_bytes.assert_not_called()
