# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Bind AArch64 lscpu inspection to the active toolkit session."""

from __future__ import annotations

import re

import gdb

from ...diagnostic_runtime import GdbDiagnosticRegisterReader
from ...ocd import OcdIdentifier, get_ocd
from ...ocd.openocd import read_external_debug_word
from ...session import SESSION, ToolkitSession
from .cpu import EXTERNAL_MIDR_OFFSET, CpuRegister, CpuReport, collect_cpu_report
from .target import AArch64TargetDescription

_JLINK_CP15_READS = {
    "MIDR_EL1": ("0,0,0,0", 64),
    "REVIDR_EL1": ("0,0,0,6", 32),
    "CTR_EL0": ("0,0,3,1", 32),
}


class GdbCpuRegisterReader(GdbDiagnosticRegisterReader):
    """Prefer named GDB registers and use verified backend-specific identity access."""

    def read_register(self, name: str, width_bits: int) -> CpuRegister:
        """Retain partial CP15 results instead of fabricating the high 32 bits."""
        value = self.read_first((name.lower(), name))
        if value is not None:
            return CpuRegister(name, width_bits, value & ((1 << width_bits) - 1))
        if name == "CurrentEL":
            pstate = self.read_first(("pstate", "cpsr"))
            if pstate is not None and not pstate & 0x10:
                return CpuRegister(name, width_bits, pstate & 0xC, source="GDB PSTATE")
        if name == "MIDR_EL1" and get_ocd().identifier is OcdIdentifier.OPENOCD:
            value = read_external_debug_word(EXTERNAL_MIDR_OFFSET)
            if value is not None:
                return CpuRegister(name, width_bits, value, source="OpenOCD external debug MIDR")
        alias = _JLINK_CP15_READS.get(name)
        if alias is not None and get_ocd().identifier is OcdIdentifier.JLINK:
            encoding, valid_bits = alias
            try:
                output = gdb.execute(f"monitor cp15 {encoding}", to_string=True)
            except gdb.error:
                output = ""
            match = re.fullmatch(
                rf"\s*Reading CP15 register \({encoding} = (0x[0-9A-Fa-f]{{1,8}})\)\s*",
                output,
            )
            if match is not None:
                return CpuRegister(name, width_bits, int(match[1], 16), valid_bits, "J-Link CP15")
        return CpuRegister(name, width_bits, None)


def cpu_report(session: ToolkitSession = SESSION) -> CpuReport:
    """Collect fresh registers for the selected CPU without caching execution state."""
    target = session.require_target_of(AArch64TargetDescription)
    return collect_cpu_report(target, GdbCpuRegisterReader())
