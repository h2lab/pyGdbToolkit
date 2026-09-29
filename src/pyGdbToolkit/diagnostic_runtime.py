# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""GDB adapters for architecture-neutral diagnostic runtime contracts."""

from __future__ import annotations

import gdb

from .arch.diagnostics import (
    DiagnosticRegisterReader,
    DiagnosticRuntimeAccess,
    DiagnosticSymbolResolver,
)


class GdbDiagnosticRegisterReader:
    """Read runtime registers from GDB's selected frame."""

    def read_first(self, names: tuple[str, ...]) -> int | None:
        """Return the first GDB register exposed under the supplied names."""
        try:
            frame = gdb.selected_frame()
        except gdb.error:
            return None
        for name in names:
            try:
                return int(frame.read_register(name))
            except (gdb.error, ValueError, TypeError):
                continue
        return None


class GdbDiagnosticSymbolResolver:
    """Resolve target addresses through GDB without architecture assumptions."""

    def resolve(self, address: int) -> str:
        """Return GDB's symbol description or ``"?"`` when unavailable."""
        try:
            output = gdb.execute(f"info symbol 0x{address:X}", to_string=True).strip()
        except (gdb.error, TypeError, ValueError):
            return "?"
        if "No symbol matches" in output or "not in valid memory" in output:
            return "?"
        return output.split(" in section ")[0]


def gdb_diagnostic_access() -> DiagnosticRuntimeAccess:
    """Create portable diagnostic access adapters backed by the selected GDB frame."""
    registers: DiagnosticRegisterReader = GdbDiagnosticRegisterReader()
    symbols: DiagnosticSymbolResolver = GdbDiagnosticSymbolResolver()
    return DiagnosticRuntimeAccess(registers=registers, symbols=symbols)
