# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Cortex-M address-space hints, never a physical device memory map."""

from __future__ import annotations

from typing import Sequence

from ...target_memory import TargetMemory, TargetReadError
from ..memmap import (
    ExecutionHint,
    GenericMemoryMapProvider,
    MemoryRegion,
    MemoryBaseline,
    RuntimeMemoryEvidence,
    TargetFingerprint,
)
from .coresight import discover_rom_tables
from .cortex_m import CPUID_ADDRESS, ScbRegister, decode_cpuid
from .providers import DEFAULT_PROVIDER_REGISTRY


class CortexMMemoryMapProvider:
    """Describe architectural windows without inventing Flash, RAM or OTP sizes."""

    def supports(self, architecture: str) -> bool:
        """Restrict these windows to explicit M-profile GDB architecture names."""
        return architecture.lower().startswith(
            ("armv6-m", "armv6s-m", "armv7-m", "armv7e-m", "armv8-m", "armv8.1-m")
        )

    def describe(self, start: int, end: int) -> str:
        """Return a candidate bus window, not an assertion of physical memory."""
        windows = (
            (0x00000000, 0x20000000, "Cortex-M code space"),
            (0x20000000, 0x40000000, "Cortex-M SRAM space"),
            (0x40000000, 0x60000000, "Cortex-M peripheral space"),
            (0x60000000, 0xA0000000, "Cortex-M external RAM space"),
            (0xA0000000, 0xE0000000, "Cortex-M external device space"),
            (0xE0000000, 0x100000000, "Cortex-M system space"),
        )
        return next(
            (name for lower, upper, name in windows if lower <= start < end <= upper),
            "unknown address space",
        )

    def fingerprint(
        self, memory: TargetMemory, architecture: str, server: TargetFingerprint | None
    ) -> TargetFingerprint | None:
        """Reuse manufacturer recognition from a validated, unambiguous MCU ROM."""
        target = decode_cpuid(memory.read_uint32(CPUID_ADDRESS))
        discovery = discover_rom_tables(memory)
        table = discovery.mcu_rom.table
        if table is None or table.identity.peripheral_id.jep106 is None:
            return server
        report = DEFAULT_PROVIDER_REGISTRY.inspect(memory, target, discovery)
        product = report.product_line.value
        if not product or product.startswith("Ambiguous:") or not report.part_number.is_available:
            return server
        peripheral = table.identity.peripheral_id
        evidence: tuple[str, ...] = (
            f"MCU ROM {table.base:#x}: JEP106 {peripheral.jep106}, part {peripheral.part_number:#x}",
            f"Unique manufacturer provider match: {product}",
            f"CPUID: {target.core_name}",
        )
        confidence = "hardware-confirmed"
        if server is not None:
            if (
                server.vendor.casefold() != report.vendor.casefold()
                or not server.soc.casefold().startswith(product.split()[0].casefold())
            ):
                confidence = "conflict"
                evidence += ("Server identity conflicts with hardware product line",)
        return TargetFingerprint(
            report.vendor,
            product,
            architecture,
            confidence,
            evidence,
            server_soc=None if server is None else server.soc,
        )

    def runtime_evidence(
        self,
        memory: TargetMemory,
        registers: dict[str, int],
        fingerprint: TargetFingerprint | None,
        regions: Sequence[MemoryRegion],
    ) -> RuntimeMemoryEvidence:
        """Inspect active vector tables and mandatory handlers with bounded reads."""
        result = GenericMemoryMapProvider().runtime_evidence(
            memory, registers, fingerprint, regions
        )
        for hint in tuple(result.hints):
            if hint.role == "code" and hint.address & 1:
                result.hints.remove(hint)
                result.hints.append(
                    ExecutionHint(
                        hint.name,
                        hint.address & ~1,
                        hint.role,
                        hint.evidence + "; Thumb bit cleared",
                    )
                )
        try:
            target = decode_cpuid(memory.read_uint32(CPUID_ADDRESS))
        except (TargetReadError, ValueError) as error:
            result.notes.append(f"Runtime vector discovery unavailable: {error}")
            return result
        if target.core is not None and target.core.architecture_version.value.startswith("Armv8"):
            result.candidates.append(
                MemoryBaseline(
                    "architecture-dependent",
                    "ARMv8-M SoC layouts",
                    "Cortex-M",
                    0x18000000,
                    0x19000000,
                    "rom-or-flash",
                    "Implementation-dependent SoC memory maps",
                    "Possible ROM/Flash or secure code alias; not mandated by ARMv8-M; window bounds are search hypotheses, not physical capacity; presence and alias relationships require evidence",
                )
            )
        if target.core is None or ScbRegister.VTOR not in target.core.scb_registers:
            result.notes.append("VTOR not assumed for a core without a standard VTOR")
            return result
        tables = [("VTOR", 0xE000ED08)]
        if target.core.sau is not None:
            tables.append(("VTOR_NS", 0xE002ED08))
        for name, register_address in tables:
            try:
                base = memory.read_uint32(register_address)
            except TargetReadError as error:
                result.notes.append(f"{name} unavailable: {error}")
                continue
            if base & 0x7F:
                result.notes.append(f"{name} has invalid alignment: {base:#x}")
                continue
            result.hints.append(
                ExecutionHint(
                    name,
                    base,
                    "vector-table",
                    f"SCB register {register_address:#x}; current table base, not physical memory base",
                )
            )
            excluded = any(
                region.kind in ("registers", "otp")
                and region.start < base + 16
                and base < region.end
                for region in regions
            )
            permitted = base < 0x40000000 or 0x60000000 <= base <= 0x9FFFFFF0
            if not permitted or excluded:
                result.notes.append(f"{name} vectors not read: address may be MMIO/OTP")
                continue
            for offset, label, role in (
                (0, "InitialSP", "stack-initial"),
                (4, "Reset", "handler"),
                (8, "NMI", "handler"),
                (12, "HardFault", "handler"),
            ):
                try:
                    value = memory.read_uint32(base + offset)
                except TargetReadError as error:
                    result.notes.append(f"{name}.{label} unavailable: {error}")
                    continue
                if role == "handler":
                    if value in (0, 0xFFFFFFFF) or not value & 1:
                        result.notes.append(f"{name}.{label} is not a valid Thumb handler pointer")
                        continue
                    value &= ~1
                    if not (value < 0x40000000 or 0x60000000 <= value < 0xA0000000):
                        result.notes.append(f"{name}.{label} points outside candidate code space")
                        continue
                elif value == 0 or value == 0xFFFFFFFF or value & 3:
                    result.notes.append(f"{name}.{label} is not an aligned initial stack pointer")
                    continue
                result.hints.append(
                    ExecutionHint(
                        f"{name}.{label}",
                        value,
                        role,
                        f"Vector entry at {base + offset:#x}; pointer only, not proof of present handler/stack access",
                    )
                )
        return result
