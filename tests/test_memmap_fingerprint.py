"""Fingerprint provenance, contradictions and local GDB-policy restoration."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pyGdbToolkit.arch.arm import memmap as arm_memmap
from pyGdbToolkit.arch.arm.models import FieldValue
from pyGdbToolkit.arch.memmap import MemoryBaseline, MemoryRegion, TargetFingerprint
from pyGdbToolkit.memmap import MemoryMapReport
from pyGdbToolkit.memmap_runtime import (
    gdb_memory_policy,
    gdb_target_fingerprint,
    parse_target_identity,
)

IDENTITY = "Target type: stm32n657i0hxq\nVendor: STMicroelectronics\nPart number: STM32N657I0HxQ\n"


def test_server_identity_is_not_hardware_confirmation():
    """A configured target name is useful evidence, not a hardware fingerprint."""
    fingerprint = parse_target_identity(IDENTITY, "armv8.1-m.main")
    assert fingerprint.soc == "STM32N657I0HxQ"
    assert not fingerprint.confirmed
    assert parse_target_identity("Vendor: STMicroelectronics", "arm") is None
    assert parse_target_identity(IDENTITY.replace("stm32n657i0hxq", "stm32f407"), "arm") is None


@pytest.fixture
def hardware(monkeypatch):
    """Supply the exact independently decoded hardware facts observed on the board."""
    target = SimpleNamespace(core_name="Cortex-M55")
    table = SimpleNamespace(
        base=0xE00FE000,
        identity=SimpleNamespace(
            peripheral_id=SimpleNamespace(jep106=(0, 0x20), part_number=0x486)
        ),
    )
    discovery = SimpleNamespace(mcu_rom=SimpleNamespace(table=table))
    report = SimpleNamespace(
        vendor="STMicroelectronics",
        product_line=FieldValue.known("STM32N6 product line"),
        part_number=FieldValue.known("STM32N6 (exact ordering code unavailable)"),
    )
    monkeypatch.setattr(arm_memmap, "decode_cpuid", lambda _: target)
    monkeypatch.setattr(arm_memmap, "discover_rom_tables", lambda _: discovery)
    monkeypatch.setattr(arm_memmap.DEFAULT_PROVIDER_REGISTRY, "inspect", lambda *args: report)
    return report, discovery


def test_hardware_fingerprint_preserves_precision(hardware):
    """Confirm the hardware family, but retain the exact server part as server evidence."""
    fingerprint = arm_memmap.CortexMMemoryMapProvider().fingerprint(
        Mock(), "armv8.1-m.main", parse_target_identity(IDENTITY, "armv8.1-m.main")
    )
    assert fingerprint.confirmed
    assert fingerprint.soc == "STM32N6 product line"
    assert fingerprint.server_soc == "STM32N657I0HxQ"


@pytest.mark.parametrize("vendor,soc", [("NXP", "LPC55"), ("STMicroelectronics", "STM32F407")])
def test_conflicting_server_is_not_confirmed(hardware, vendor, soc):
    """Do not publish an exact configured part that contradicts the live hardware."""
    server = TargetFingerprint(vendor, soc, "arm", "server-reported", ("configuration",))
    fingerprint = arm_memmap.CortexMMemoryMapProvider().fingerprint(Mock(), "arm", server)
    assert fingerprint.confidence == "conflict"
    assert not fingerprint.confirmed


def test_ambiguous_hardware_is_not_confirmed(hardware):
    """Shared peripheral identities cannot establish a unique device family."""
    report, _ = hardware
    report.product_line = FieldValue.known("Ambiguous: STM32 families")
    server = parse_target_identity(IDENTITY, "arm")
    assert arm_memmap.CortexMMemoryMapProvider().fingerprint(Mock(), "arm", server) is server


@pytest.mark.parametrize("initial", [True, False])
def test_local_policy_restored_on_failure(fake_gdb, monkeypatch, initial):
    """Restore both original policy states even when a read or fingerprint fails."""
    execute = Mock()
    monkeypatch.setattr(fake_gdb, "parameter", lambda _: initial, raising=False)
    monkeypatch.setattr(fake_gdb, "execute", execute, raising=False)
    with pytest.raises(RuntimeError):
        with gdb_memory_policy(True):
            raise RuntimeError("access failed")
    assert [call.args[0] for call in execute.call_args_list] == [
        "set mem inaccessible-by-default off",
        "set mem inaccessible-by-default " + ("on" if initial else "off"),
    ]


def test_generic_architecture_does_not_read_hardware(fake_gdb, monkeypatch):
    """A future non-ARM target can retain metadata without speculative CPUID reads."""
    execute = Mock(return_value=IDENTITY)
    monkeypatch.setattr(fake_gdb, "execute", execute, raising=False)
    fingerprint = gdb_target_fingerprint("riscv", {"backend": "PyOcdMonitorTransport"})
    assert not fingerprint.confirmed
    assert execute.call_count == 1


def test_candidates_are_challenged_not_promoted():
    """Record type conflicts and absent evidence without asserting absence or protection."""
    candidate = MemoryBaseline("test", "family", "unknown", 0, 0x100, "flash", "maps", "candidate")
    report = MemoryMapReport(
        "unknown", {}, [MemoryRegion(0, 0x10, "ram", "gdb-server")], candidates=[candidate]
    )
    assert report.candidate_assessments()[0]["status"] == "kind-conflict"
    report.regions.clear()
    assert report.candidate_assessments()[0]["status"] == "unconfirmed"
