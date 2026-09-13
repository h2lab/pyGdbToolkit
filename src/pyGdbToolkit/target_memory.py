# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Typed target-memory access shared by GDB commands."""

from __future__ import annotations

from typing import Protocol

import gdb


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
