"""PowerPC / MPC embedded microcontroller register access implementations (stub)."""

from __future__ import annotations

import gdb

from ..registers import BaseRegisterAccessor


class PowerPcRegisterAccessor(BaseRegisterAccessor):
    """PowerPC / MPC embedded microcontroller register accessor (stub)."""

    def __init__(self, frame: gdb.Frame | None = None, arch_name: str = "powerpc") -> None:
        super().__init__(frame=frame, bit_mask=0xFFFFFFFF, arch_name=arch_name)

    def pc(self) -> int:
        return self.read("pc")

    def sp(self) -> int:
        return self.read("r1")  # r1 is ABI stack pointer on PowerPC

    def lr(self) -> int | None:
        return self.read_optional("lr")

    def flags(self) -> int | None:
        return self.read_optional("msr")

    def general_registers(self) -> dict[str, int]:
        names = [f"r{i}" for i in range(32)]
        return self.read_multiple(names)
