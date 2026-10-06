# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Identify the connected on-chip debugger without registering a GDB command."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Callable

import gdb


class OcdIdentifier(StrEnum):
    """Stable identifiers for supported debug servers."""

    UNKNOWN = "unknown"
    PYOCD = "pyocd"
    OPENOCD = "openocd"
    JLINK = "jlink"


@dataclass(frozen=True)
class OcdInfo:
    """Detected server identity and evidence, or an explicit unknown result."""

    identifier: OcdIdentifier = OcdIdentifier.UNKNOWN
    version: str | None = None
    evidence: str = ""


def probe_ocd(execute: Callable[[str], str]) -> OcdInfo:
    """Identify an OCD using only non-mutating monitor requests."""
    errors: list[str] = []
    try:
        output = execute("monitor echo [version]")
    except Exception as error:
        errors.append(str(error))
    else:
        match = re.search(r"Open On-Chip Debugger\s+([^\r\n]+)", output, re.I)
        if match is not None:
            return OcdInfo(OcdIdentifier.OPENOCD, match[1].strip(), output.strip())
        errors.append(output.strip())
    try:
        output = execute("monitor show aps")
    except Exception as error:
        errors.append(str(error))
    else:
        if re.search(r"^\s*\d+ APs:\s*$", output, re.M):
            return OcdInfo(OcdIdentifier.PYOCD, evidence=output.strip())
        errors.append(output.strip())
    try:
        output = execute("monitor help")
    except Exception as error:
        errors.append(str(error))
    else:
        match = re.search(r"^\s*SEGGER J-Link GDB Server\s+V([^\s]+)", output, re.M | re.I)
        if match is not None:
            return OcdInfo(OcdIdentifier.JLINK, match[1], output.strip())
        errors.append(output.strip())
    return OcdInfo(evidence="; ".join(error for error in errors if error) or "OCD unavailable")


def _gdb_execute(command: str) -> str:
    return str(gdb.execute(command, to_string=True))


def _connection_key() -> tuple[int, int] | None:
    inferior = gdb.selected_inferior()
    connection = getattr(inferior, "connection", None)
    if connection is None or getattr(connection, "type", None) not in ("remote", "extended-remote"):
        return None
    return int(inferior.num), int(connection.num)


class OcdDetector:
    """Cache identity per GDB connection and retry unknown probes on demand."""

    def __init__(
        self,
        execute: Callable[[str], str] = _gdb_execute,
        connection_key: Callable[[], tuple[int, int] | None] = _connection_key,
    ) -> None:
        """Inject monitor execution and connection identity for tests."""
        self._execute = execute
        self._connection_key = connection_key
        self._key: tuple[int, int] | None = None
        self._info = OcdInfo()

    def reset(self) -> None:
        """Discard connection-specific identity."""
        self._key = None
        self._info = OcdInfo()

    def get(self, *, refresh: bool = False) -> OcdInfo:
        """Return the current OCD identity, probing when the connection changes."""
        try:
            key = self._connection_key()
        except Exception:
            key = None
        if key is None:
            self.reset()
            return self._info
        if refresh or key != self._key or self._info.identifier == OcdIdentifier.UNKNOWN:
            self._info = probe_ocd(self._execute)
            self._key = key
        return self._info


OCD = OcdDetector()


def get_ocd(*, refresh: bool = False) -> OcdInfo:
    """Expose the connected OCD's identifier and optional version to modules."""
    return OCD.get(refresh=refresh)


def initialize_ocd() -> OcdInfo:
    """Attempt detection at toolkit startup and invalidate on connection removal."""
    events = getattr(gdb, "events", None)
    registry = getattr(events, "connection_removed", None)
    if registry is not None:
        registry.connect(_connection_removed)
    return get_ocd()


def _connection_removed(event: object) -> None:
    del event
    OCD.reset()
