# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Portable memory-map sampling contracts."""

import pytest

from pyGdbToolkit.arch.memmap import MemoryMapRegistry, MemoryRegion
from pyGdbToolkit.memmap import MemoryMapReport, probe_regions
from pyGdbToolkit.target_memory import TargetReadError


class Memory:
    """Record reads and fail at a selected address without offering writes."""

    def __init__(self):
        self.reads = []

    def read_bytes(self, address, size):
        self.reads.append((address, size))
        if address == 0x1004:
            raise TargetReadError(address, size, "bus fault")
        return bytes(size)


def test_error_is_not_protection():
    """Keep read failures separate from the declared protection state."""
    report = MemoryMapReport("riscv", {}, [MemoryRegion(0x1000, 0x1010, "ram", "server")])
    memory = Memory()
    probe_regions(report, memory, report.regions, stride=4, max_reads=2)
    assert memory.reads == [(0x1000, 4), (0x1004, 4)]
    assert [sample.status for sample in report.observations] == ["readable-sampled", "read-error"]
    assert report.regions[0].protection == "unknown"
    assert report.probe_runs[0]["stop_reason"] == "max-reads"


def test_overlaps_and_short_tail():
    """Read overlapping points only once and never read past a range end."""
    report = MemoryMapReport("unknown", {})
    memory = Memory()
    ranges = [MemoryRegion(0, 5, "unknown", "manual"), MemoryRegion(0, 8, "ram", "server")]
    probe_regions(report, memory, ranges, stride=4)
    assert memory.reads == [(0, 4), (4, 1)]


def test_deadline():
    """Stop before a read once the global deadline expires."""
    report = MemoryMapReport("unknown", {})
    memory = Memory()
    ticks = iter([0, 1, 6])
    probe_regions(
        report, memory, [MemoryRegion(0, 100, "ram", "server")], stride=4, clock=lambda: next(ticks)
    )
    assert len(memory.reads) == 1
    assert report.probe_runs[0]["stop_reason"] == "timeout"


@pytest.mark.parametrize(
    "options", [{"stride": 0}, {"stride": 3}, {"max_reads": 0}, {"timeout": float("nan")}]
)
def test_bad_limits(options):
    """Reject invalid budgets before reading anything."""
    memory = Memory()
    with pytest.raises(ValueError):
        probe_regions(MemoryMapReport("unknown", {}), memory, [], **options)
    assert not memory.reads


def test_generic_provider():
    """Unknown architectures remain usable without ARM assumptions."""
    assert (
        MemoryMapRegistry().provider("riscv").describe(0x20000000, 0x20001000)
        == "unknown address space"
    )
