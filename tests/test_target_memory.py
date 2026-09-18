"""Tests for typed selected-inferior memory access."""

from __future__ import annotations

import pytest

from pyGdbToolkit.target_memory import TargetMemoryReader, TargetReadError


class FakeInferior:
    """A byte-addressable fake GDB inferior."""

    def __init__(self, memory: dict[int, bytes], failure: Exception | None = None) -> None:
        """Initialize fixed memory ranges and an optional target error."""
        self.memory = memory
        self.failure = failure

    def read_memory(self, address: int, size: int) -> bytes:
        """Return a configured range or simulate an inaccessible target."""
        if self.failure is not None:
            raise self.failure
        return self.memory.get(address, b"")[:size]


def test_reads_little_endian_values_from_selected_inferior(fake_gdb: object) -> None:
    """The reader selects the current inferior and decodes little endian."""
    del fake_gdb
    import gdb

    gdb._inferior = FakeInferior(  # type: ignore[attr-defined]
        {
            0x1000: b"\x34\x12",
            0x2000: b"\x78\x56\x34\x12",
        }
    )
    reader = TargetMemoryReader()

    assert reader.read_uint16(0x1000) == 0x1234
    assert reader.read_uint32(0x2000) == 0x12345678


def test_wraps_target_failures_with_access_context(fake_gdb: object) -> None:
    """A target access error includes the requested address and size."""
    del fake_gdb
    import gdb

    reader = TargetMemoryReader(FakeInferior({}, gdb.MemoryError("memory fault")))

    with pytest.raises(TargetReadError, match=r"2 byte\(s\) at 0x00001000: memory fault"):
        reader.read_uint16(0x1000)


def test_rejects_invalid_memory_ranges() -> None:
    """Invalid target ranges fail before GDB is called."""
    reader = TargetMemoryReader(FakeInferior({}))

    with pytest.raises(ValueError, match="negative"):
        reader.read_bytes(-1, 1)
    with pytest.raises(ValueError, match="positive"):
        reader.read_bytes(0, 0)


def test_rejects_short_target_reads() -> None:
    """A short target range cannot silently become a decoded value."""
    reader = TargetMemoryReader(FakeInferior({0x2000: b"\x00"}))

    with pytest.raises(TargetReadError, match="target returned 1 byte"):
        reader.read_uint32(0x2000)


def test_wraps_missing_selected_inferior(fake_gdb: object) -> None:
    """Selecting an inferior also reports a contextual target-read error."""
    del fake_gdb

    with pytest.raises(TargetReadError, match="could not select inferior"):
        TargetMemoryReader()
