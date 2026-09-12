"""AVR 8-bit architecture register access implementations (stub)."""

from __future__ import annotations

import gdb

from ..registers import BaseRegisterAccessor


class AvrRegisterAccessor(BaseRegisterAccessor):
    """AVR 8-bit / microcontroller register accessor (stub)."""

    def __init__(self, frame: gdb.Frame | None = None, arch_name: str = "avr") -> None:
        super().__init__(frame=frame, bit_mask=0xFFFF, arch_name=arch_name)

    def pc(self) -> int:
        return self.read("pc")

    def sp(self) -> int:
        return self.read("sp")

    def lr(self) -> int | None:
        return None  # AVR does not have a dedicated LR

    def flags(self) -> int | None:
        return self.read_optional("sreg")

    def general_registers(self) -> dict[str, int]:
        names = [f"r{i}" for i in range(32)]
        return self.read_multiple(names)
