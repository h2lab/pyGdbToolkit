# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Typed target-memory access shared by GDB commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

import gdb


@dataclass(frozen=True)
class PhysicalMemoryRange:
    """An explicitly authorized physical table-memory interval, end excluded."""

    start: int
    end: int

    def __post_init__(self) -> None:
        """Reject empty, negative or overflowing authorization intervals."""
        if not 0 <= self.start < self.end <= 1 << 64:
            raise ValueError("invalid physical memory range")


class PhysicalTableMemory(Protocol):
    """Explicit physical access, not ordinary GDB virtual memory or a guessed MEM-AP."""

    source: str
    table_ranges: tuple[PhysicalMemoryRange, ...]
    stage1_addresses_are_physical: bool

    def snapshot_is_valid(self) -> bool:
        """Verify the bound CPU/security context and stable halted/coherent table snapshot."""
        ...

    def read_physical_bytes(self, address: int, size: int) -> bytes:
        """Read physical table bytes or raise TargetReadError, with no address translation."""
        ...


@dataclass(frozen=True)
class RestrictedPhysicalTableMemory:
    """Adapt a verified backend callback with explicit authorization and context checks."""

    source: str
    table_ranges: tuple[PhysicalMemoryRange, ...]
    read: Callable[[int, int], bytes]
    valid_context: Callable[[], bool]
    stage1_addresses_are_physical: bool = False

    def snapshot_is_valid(self) -> bool:
        """Delegate context, halt-state and coherence verification to the backend."""
        return self.valid_context()

    def read_physical_bytes(self, address: int, size: int) -> bytes:
        """Reject stale contexts, unauthorized ranges and short physical reads."""
        if size <= 0 or address < 0 or address + size > 1 << 64:
            raise ValueError("invalid physical read range")
        if not self.snapshot_is_valid() or not self.stage1_addresses_are_physical:
            raise TargetReadError(address, size, "physical table context is not verified")
        if not any(
            region.start <= address and address + size <= region.end for region in self.table_ranges
        ):
            raise TargetReadError(address, size, "physical table read is not authorized")
        result = self.read(address, size)
        if len(result) != size:
            raise TargetReadError(address, size, "short physical table read")
        return result


class TargetMemory(Protocol):
    """The integer-oriented target-memory operations used by providers."""

    def read_bytes(self, address: int, size: int) -> bytes:
        """Read an exact number of bytes from target memory."""
        ...

    def read_uint16(self, address: int) -> int:
        """Read a little-endian unsigned 16-bit integer."""
        ...

    def read_uint32(self, address: int) -> int:
        """Read a little-endian unsigned 32-bit integer."""
        ...


@runtime_checkable
class WritableTargetMemory(TargetMemory, Protocol):
    """Target-memory operations that can also write 32-bit register values."""

    def write_uint32(self, address: int, value: int) -> None:
        """Write a little-endian unsigned 32-bit integer."""
        ...


class TargetReadError(RuntimeError):
    """An error enriched with the target-memory range that could not be read."""

    def __init__(self, address: int, size: int, reason: str) -> None:
        """Initialize an error for one failed target-memory operation.

        Parameters
        ----------
        address
            First target address that was requested.
        size
            Number of bytes requested.
        reason
            The underlying target-access failure.
        """
        self.address = address
        self.size = size
        self.reason = reason
        super().__init__(f"could not read {size} byte(s) at 0x{address:08X}: {reason}")


class TargetWriteError(RuntimeError):
    """An error enriched with the target-memory range that could not be written."""

    def __init__(self, address: int, size: int, reason: str) -> None:
        """Initialize an error for one failed target-memory write.

        Parameters
        ----------
        address
            First target address that was requested.
        size
            Number of bytes requested.
        reason
            The underlying target-access failure.
        """
        self.address = address
        self.size = size
        self.reason = reason
        super().__init__(f"could not write {size} byte(s) at 0x{address:08X}: {reason}")


class TargetMemoryReader:
    """Read little-endian values from GDB's selected inferior."""

    def __init__(self, inferior: gdb.Inferior | None = None) -> None:
        """Create a reader for an inferior.

        Parameters
        ----------
        inferior
            An optional GDB inferior. The selected inferior is used by default.
        """
        if inferior is not None:
            self._inferior = inferior
            return
        try:
            self._inferior = gdb.selected_inferior()
        except gdb.error as error:
            raise TargetReadError(0, 0, f"could not select inferior: {error}") from error

    @property
    def architecture_name(self) -> str | None:
        """Return the bound inferior's GDB architecture without accessing memory."""
        architecture = getattr(self._inferior, "architecture", None)
        if architecture is None:
            return None
        try:
            return str(architecture().name())
        except gdb.error:
            return None

    def read_bytes(self, address: int, size: int) -> bytes:
        """Read an exact byte range from the target.

        Parameters
        ----------
        address
            Target address to read.
        size
            Number of bytes to read.

        Returns
        -------
        bytes
            The bytes returned by the selected inferior.

        Raises
        ------
        TargetReadError
            If GDB rejects the read or returns an incomplete range.
        ValueError
            If the requested range is invalid.
        """
        if address < 0:
            raise ValueError("target address must not be negative")
        if size <= 0:
            raise ValueError("target-memory read size must be positive")

        try:
            result = bytes(self._inferior.read_memory(address, size))
        except (gdb.error, gdb.MemoryError) as error:
            raise TargetReadError(address, size, str(error)) from error

        if len(result) != size:
            raise TargetReadError(address, size, f"target returned {len(result)} byte(s)")
        return result

    def read_uint16(self, address: int) -> int:
        """Read a little-endian unsigned 16-bit value.

        Parameters
        ----------
        address
            Target address to read.

        Returns
        -------
        int
            The decoded value.
        """
        return int.from_bytes(self.read_bytes(address, 2), byteorder="little")

    def read_uint32(self, address: int) -> int:
        """Read a little-endian unsigned 32-bit value.

        Parameters
        ----------
        address
            Target address to read.

        Returns
        -------
        int
            The decoded value.
        """
        return int.from_bytes(self.read_bytes(address, 4), byteorder="little")

    def write_uint32(self, address: int, value: int) -> None:
        """Write an unsigned 32-bit value to target memory in little-endian order.

        Parameters
        ----------
        address
            Target address to write.
        value
            Unsigned 32-bit value to write.

        Raises
        ------
        TargetWriteError
            If GDB rejects the write.
        ValueError
            If the target address or value is invalid.
        """
        if address < 0:
            raise ValueError("target address must not be negative")
        if not 0 <= value <= 0xFFFFFFFF:
            raise ValueError("target-memory write value must be an unsigned 32-bit value")

        try:
            self._inferior.write_memory(address, value.to_bytes(4, byteorder="little"))
        except (gdb.error, gdb.MemoryError) as error:
            raise TargetWriteError(address, 4, str(error)) from error
