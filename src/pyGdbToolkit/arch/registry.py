# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Architecture probe registry."""

from __future__ import annotations

from typing import Sequence

from ..target_memory import TargetMemory
from .base import ArchitectureProbe, ProbeResult


class ArchitectureRegistry:
    """Run registered architecture probes in their declared priority order."""

    def __init__(self, probes: Sequence[ArchitectureProbe]) -> None:
        """Create a registry from an immutable probe sequence."""
        self._probes = tuple(probes)

    def probe(self, reader: TargetMemory) -> ProbeResult:
        """Return the first successful architecture probe result.

        Parameters
        ----------
        reader
            Target-memory reader used by each architecture probe.

        Returns
        -------
        ProbeResult
            The first detected target or an explicit unsupported result.
        """
        for probe in self._probes:
            result = probe.probe(reader)
            if result.is_available:
                return result
        return ProbeResult.unavailable("no registered architecture probe recognized the target")
