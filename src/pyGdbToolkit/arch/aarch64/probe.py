# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Recognize AArch64 from GDB metadata without probing Cortex-M registers."""

from __future__ import annotations

from ...target_memory import TargetMemory
from ..base import Architecture, ProbeResult, SystemRegisterSet, TargetDescription
from .target import AArch64TargetDescription


class AArch64Probe:
    """Registry adapter for GDB targets explicitly identified as AArch64."""

    architecture = Architecture.AARCH64

    def probe(self, reader: TargetMemory) -> ProbeResult:
        """Identify the execution architecture without reading target memory."""
        architecture_name = getattr(reader, "architecture_name", None)
        if not isinstance(architecture_name, str):
            return ProbeResult.unavailable("GDB architecture is unavailable")
        normalized_name = architecture_name.lower()
        if normalized_name != "aarch64" and not normalized_name.startswith("aarch64:"):
            return ProbeResult.unavailable("GDB architecture does not identify AArch64")
        return ProbeResult.detected(
            AArch64TargetDescription(
                architecture=Architecture.AARCH64,
                family="Arm",
                core_name="AArch64",
                revision="unknown",
                gdb_architecture=architecture_name,
            )
        )

    def read_system_registers(
        self,
        reader: TargetMemory,
        target: TargetDescription,
    ) -> SystemRegisterSet:
        """Return no registers until AArch64 system-register access is implemented."""
        del reader
        if not isinstance(target, AArch64TargetDescription):
            raise ValueError("AArch64Probe requires an AArch64TargetDescription")
        return SystemRegisterSet(Architecture.AARCH64, "AArch64 system registers", ())


DEFAULT_AARCH64_PROBES = (AArch64Probe(),)
