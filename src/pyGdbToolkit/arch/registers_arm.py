"""ARM Cortex-M and ARM architecture register access implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import gdb

from ..registers import BaseRegisterAccessor


@dataclass(frozen=True)
class APSRFlags:
    """ARM Application Program Status Register (APSR) condition flags."""

    raw: int
    n: bool  # Negative condition flag (bit 31)
    z: bool  # Zero condition flag (bit 30)
    c: bool  # Carry condition flag (bit 29)
    v: bool  # Overflow condition flag (bit 28)
    q: bool  # Sticky saturation flag (bit 27)
    ge: int  # Greater than or Equal flags (bits 19:16)

    @classmethod
    def from_raw(cls, raw: int) -> APSRFlags:
        return cls(
            raw=raw,
            n=bool((raw >> 31) & 1),
            z=bool((raw >> 30) & 1),
            c=bool((raw >> 29) & 1),
            v=bool((raw >> 28) & 1),
            q=bool((raw >> 27) & 1),
            ge=(raw >> 16) & 0xF,
        )


@dataclass(frozen=True)
class ControlRegister:
    """ARM Cortex-M CONTROL register flags."""

    raw: int
    npriv: bool  # 0 = Privileged, 1 = Unprivileged (bit 0)
    spsel: bool  # 0 = MSP, 1 = PSP in Thread mode (bit 1)
    fpca: bool   # 0 = FP extension inactive, 1 = active (bit 2)

    @classmethod
    def from_raw(cls, raw: int) -> ControlRegister:
        return cls(
            raw=raw,
            npriv=bool((raw >> 0) & 1),
            spsel=bool((raw >> 1) & 1),
            fpca=bool((raw >> 2) & 1),
        )


@dataclass(frozen=True)
class CoreRegistersARM:
    """Snapshot of standard ARM Cortex-M core registers."""

    r0: int
    r1: int
    r2: int
    r3: int
    r4: int
    r5: int
    r6: int
    r7: int
    r8: int
    r9: int
    r10: int
    r11: int
    r12: int
    sp: int
    lr: int
    pc: int
    xpsr: int


class ArmRegisterAccessor(BaseRegisterAccessor):
    """ARM and ARM Cortex-M specialized register accessor."""

    ARM_ALIASES: Mapping[str, str] = {
        "r13": "sp",
        "r14": "lr",
        "r15": "pc",
        "cpsr": "xpsr",
        "psr": "xpsr",
    }

    def __init__(
        self,
        frame: gdb.Frame | None = None,
        bit_mask: int = 0xFFFFFFFF,
        arch_name: str = "arm",
    ) -> None:
        super().__init__(frame=frame, bit_mask=bit_mask, arch_name=arch_name)

    def resolve_alias(self, name: str) -> str:
        return self.ARM_ALIASES.get(name, name)

    def pc(self) -> int:
        return self.read("pc")

    def sp(self) -> int:
        _, val = self.read_active_sp()
        return val

    def lr(self) -> int | None:
        return self.read_optional("lr")

    def flags(self) -> int | None:
        for candidate in ("xpsr", "apsr", "cpsr"):
            val = self.read_optional(candidate)
            if val is not None:
                return val
        return None

    def general_registers(self) -> dict[str, int]:
        names = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc"]
        return self.read_multiple(names)

    def read_arm_core_registers(self) -> CoreRegistersARM:
        """Read all standard ARM Cortex-M general-purpose registers."""
        names = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "xpsr"]
        regs = self.read_multiple(names)
        return CoreRegistersARM(
            r0=regs["r0"],
            r1=regs["r1"],
            r2=regs["r2"],
            r3=regs["r3"],
            r4=regs["r4"],
            r5=regs["r5"],
            r6=regs["r6"],
            r7=regs["r7"],
            r8=regs["r8"],
            r9=regs["r9"],
            r10=regs["r10"],
            r11=regs["r11"],
            r12=regs["r12"],
            sp=regs["sp"],
            lr=regs["lr"],
            pc=regs["pc"],
            xpsr=regs["xpsr"],
        )

    def read_active_sp(self) -> tuple[str, int]:
        """Determine and read the currently active stack pointer (MSP or PSP).

        In Cortex-M, Handler mode (IPSR != 0) always uses MSP.
        Thread mode uses PSP if CONTROL.SPSEL is 1, otherwise MSP.
        """
        ipsr = self.read_optional("ipsr")
        if ipsr is None:
            xpsr = self.read_optional("xpsr")
            ipsr = (xpsr & 0x1FF) if xpsr is not None else 0

        in_handler_mode = (ipsr != 0)
        if in_handler_mode:
            return "msp", self.read_optional("msp", self.read("sp"))

        control = self.read_optional("control")
        spsel = bool((control >> 1) & 1) if control is not None else False
        if spsel:
            return "psp", self.read_optional("psp", self.read("sp"))
        return "msp", self.read_optional("msp", self.read("sp"))

    def read_apsr(self) -> APSRFlags:
        """Read and decode APSR / xPSR condition flags."""
        val = self.read_optional("apsr")
        if val is None:
            val = self.read("xpsr")
        return APSRFlags.from_raw(val)

    def read_control(self) -> ControlRegister | None:
        """Read and decode the Cortex-M CONTROL register."""
        val = self.read_optional("control")
        if val is None:
            return None
        return ControlRegister.from_raw(val)

    def is_thumb_mode(self) -> bool:
        """Return True if the CPU is in Thumb execution state (xPSR/CPSR T-bit)."""
        xpsr = self.flags()
        if xpsr is None:
            return True  # Cortex-M is Thumb-only
        return bool((xpsr >> 24) & 1)

    def get_exception_number(self) -> int:
        """Return the active exception number (IPSR[8:0]). 0 = Thread mode."""
        ipsr = self.read_optional("ipsr")
        if ipsr is not None:
            return ipsr & 0x1FF
        xpsr = self.flags()
        if xpsr is not None:
            return xpsr & 0x1FF
        return 0
