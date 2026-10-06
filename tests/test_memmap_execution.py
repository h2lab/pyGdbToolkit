# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Execution pointers supply evidence after relocation, never invented capacities."""

from unittest.mock import Mock

from pyGdbToolkit.arch.arm.memmap import CortexMMemoryMapProvider
from pyGdbToolkit.arch.memmap import GenericMemoryMapProvider, TargetFingerprint
from pyGdbToolkit.target_memory import TargetReadError
from pyGdbToolkit.arch.memmap import ExecutionHint, MemoryRegion, RuntimeMemoryEvidence
from pyGdbToolkit.cmd_memmap import MemmapCmd
from pyGdbToolkit.memmap import MemoryMapReport
from pyGdbToolkit.session import ToolkitSession
from pyGdbToolkit.memmap_runtime import gdb_execution_evidence
import pytest


def test_relocated_vectors_and_ram_execution():
    """An application running in SRAM with a relocated VTOR remains discoverable."""
    values = {
        0xE000ED00: 0x410FC241,
        0xE000ED08: 0x24008000,
        0x24008000: 0x24010000,
        0x24008004: 0x24000101,
        0x24008008: 0x24000201,
        0x2400800C: 0x24000301,
    }
    memory = Mock()
    memory.read_uint32.side_effect = values.__getitem__
    result = CortexMMemoryMapProvider().runtime_evidence(
        memory, {"pc": 0x24001235, "sp": 0x2400F000}, None, []
    )
    hints = {hint.name: hint.address for hint in result.hints}
    assert hints["PC"] == 0x24001234
    assert hints["SP"] == 0x2400F000
    assert hints["VTOR"] == 0x24008000
    assert hints["VTOR.HardFault"] == 0x24000300
    assert hints["VTOR.InitialSP"] == 0x24010000
    assert result.regions == []
    assert result.candidates == []
    assert memory.read_uint32.call_count == 6


@pytest.mark.parametrize("cpuid", [0x411FD221, 0x410FD231])
def test_rom_flash_candidate_without_vendor_identity(cpuid):
    """Different ARMv8-M cores keep the hypothesis without a STM32N6 identity."""
    memory = Mock()
    memory.read_uint32.side_effect = lambda address: (
        cpuid
        if address == 0xE000ED00
        else (_ for _ in ()).throw(TargetReadError(address, 4, "blocked"))
    )
    result = CortexMMemoryMapProvider().runtime_evidence(memory, {"pc": 0x18003514}, None, [])
    assert result.regions == []
    assert len(result.candidates) == 1
    assert result.candidates[0].start == 0x18000000
    assert result.candidates[0].kind == "rom-or-flash"
    report = MemoryMapReport("arm", {}, execution_hints=result.hints, candidates=result.candidates)
    assert report.candidate_assessments()[0]["status"] == "runtime-supported"
    assert report.execution_assessments()[0]["status"] == "candidate-only"


def test_generic_pointers_do_not_read_memory():
    """Other architectures expose PC/SP without ARM offsets or memory type guesses."""
    memory = Mock()
    result = GenericMemoryMapProvider().runtime_evidence(
        memory, {"pc": 0x1000, "sp": 0x8000}, None, []
    )
    assert len(result.hints) == 2
    assert not result.regions
    assert not memory.mock_calls


def test_invalid_vtor_is_not_followed():
    """Peripheral addresses in VTOR must not trigger speculative MMIO reads."""
    memory = Mock()
    memory.read_uint32.side_effect = [0x410FC241, 0x40000000]
    result = CortexMMemoryMapProvider().runtime_evidence(memory, {}, None, [])
    assert memory.read_uint32.call_count == 2
    assert any("not read" in note for note in result.notes)


def test_unavailable_core_retains_pc_and_sp():
    """Register clues survive a blocked architectural-register read."""
    memory = Mock()
    memory.read_uint32.side_effect = TargetReadError(0xE000ED00, 4, "fault")
    result = CortexMMemoryMapProvider().runtime_evidence(memory, {"pc": 0x18003514}, None, [])
    assert result.hints[0].address == 0x18003514
    assert result.notes


def test_stack_end_and_runtime_associations():
    """SP can equal the exclusive end; PC never establishes ROM/Flash by itself."""
    report = MemoryMapReport("arm", {}, [MemoryRegion(0x20000000, 0x20010000, "ram", "server")])
    report.execution_hints = [
        ExecutionHint("SP", 0x20010000, "stack", "runtime"),
        ExecutionHint("PC", 0x18003514, "code", "runtime"),
    ]
    hints = report.to_dict()["execution_hints"]
    assert hints[0]["status"] == "region-associated"
    assert hints[1]["status"] == "unmapped-address"
    assert report.regions[0].kind == "ram"


def test_discover_keeps_runtime_code_as_candidate(fake_gdb, monkeypatch):
    """Code in 0x18000000 supports a general hypothesis, never a fabricated region."""
    identity = TargetFingerprint(
        "STMicroelectronics",
        "STM32N6 product line",
        "armv8.1-m.main",
        "hardware-confirmed",
        ("hardware",),
    )
    context = {"architecture": identity.architecture}
    evidence = RuntimeMemoryEvidence(
        hints=[
            ExecutionHint("PC", 0x18003514, "code", "runtime"),
            ExecutionHint("VTOR", 0x18000000, "vector-table", "SCB"),
        ],
    )
    command = MemmapCmd(
        ToolkitSession(),
        context=lambda: context,
        fingerprint=lambda *args: identity,
        execution=lambda *args: evidence,
    )
    monkeypatch.setattr(
        fake_gdb,
        "execute",
        lambda command, to_string: (
            'received: "l<memory-map/>"' if command.startswith("maintenance packet") else ""
        ),
        raising=False,
    )
    report = command.discover()
    assert command.session.discovery is report
    assert command.session.memory_regions == ()
    assert report.execution_assessments()[0]["candidates"][0]["start"] == 0x18000000
    assert report.execution_assessments()[1]["candidates"][0]["kind"] == "rom-or-flash"
    assert any(
        candidate["status"] == "runtime-supported" for candidate in report.candidate_assessments()
    )


def test_secure_and_nonsecure_vectors_keep_separate_origins():
    """Security-bank views are independent and handler Thumb validity is enforced."""
    values = {
        0xE000ED00: 0x411FD221,
        0xE000ED08: 0x18000000,
        0xE002ED08: 0x24008000,
        0x18000000: 0x24010000,
        0x18000004: 0x18000101,
        0x18000008: 0,
        0x1800000C: 0x18000300,
        0x24008000: 0x24020000,
        0x24008004: 0x24000101,
        0x24008008: 0x24000201,
        0x2400800C: 0xFFFFFFFF,
    }
    memory = Mock()
    memory.read_uint32.side_effect = values.__getitem__
    result = CortexMMemoryMapProvider().runtime_evidence(memory, {}, None, [])
    hints = {hint.name: hint.address for hint in result.hints}
    assert hints["VTOR"] == 0x18000000
    assert hints["VTOR_NS"] == 0x24008000
    assert hints["VTOR.Reset"] == 0x18000100
    assert "VTOR.NMI" not in hints and "VTOR.HardFault" not in hints
    assert "VTOR_NS.HardFault" not in hints
    assert memory.read_uint32.call_count == 11


def test_gdb_execution_registers_and_policy_restoration(fake_gdb, monkeypatch):
    """GDB captures the current frame after startup and restores map policy."""
    from pyGdbToolkit import memmap_runtime

    frame = Mock()
    frame.read_register.side_effect = lambda name: {"pc": 0x18003514, "sp": 0x241FFF00}[name]
    monkeypatch.setattr(fake_gdb, "selected_frame", lambda: frame)
    monkeypatch.setattr(memmap_runtime, "require_stopped_target", lambda: None)
    monkeypatch.setattr(memmap_runtime, "TargetMemoryReader", lambda: Mock())
    monkeypatch.setattr(fake_gdb, "parameter", lambda _: True, raising=False)
    execute = Mock()
    monkeypatch.setattr(fake_gdb, "execute", execute, raising=False)
    result = gdb_execution_evidence("riscv", None, [])
    assert [(hint.name, hint.address) for hint in result.hints] == [
        ("PC", 0x18003514),
        ("SP", 0x241FFF00),
    ]
    execute.assert_not_called()


def test_partial_vector_read_failure_is_retained():
    """A denied mandatory handler remains unknown rather than aborting discovery."""
    values = {
        0xE000ED00: 0x410FC241,
        0xE000ED08: 0x20000000,
        0x20000000: 0x20001000,
        0x20000004: 0x08000101,
        0x20000008: 0x08000201,
    }

    def read(address):
        if address not in values:
            raise TargetReadError(address, 4, "blocked")
        return values[address]

    memory = Mock()
    memory.read_uint32.side_effect = read
    result = CortexMMemoryMapProvider().runtime_evidence(memory, {}, None, [])
    assert any(hint.name == "VTOR.Reset" for hint in result.hints)
    assert any("HardFault unavailable" in note for note in result.notes)


@pytest.mark.parametrize(
    "kind,expected",
    [("rom", "declared-overlap"), ("flash", "declared-overlap"), ("ram", "kind-conflict")],
)
def test_rom_flash_candidate_is_compatible_with_either_technology(kind, expected):
    """Server evidence challenges the hypothesis without falsely choosing ROM over Flash."""
    from pyGdbToolkit.arch.memmap import MemoryBaseline

    candidate = MemoryBaseline(
        "unknown",
        "ARMv8-M SoC layouts",
        "Cortex-M",
        0x18000000,
        0x19000000,
        "rom-or-flash",
        "SoC maps",
        "candidate",
    )
    report = MemoryMapReport(
        "arm",
        {},
        [MemoryRegion(0x18000000, 0x18010000, kind, "gdb-server")],
        candidates=[candidate],
    )
    assert report.candidate_assessments()[0]["status"] == expected
    assert report.regions[0].kind == kind
