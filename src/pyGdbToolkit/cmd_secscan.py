# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""The ``secscan`` GDB command: full security-posture audit of a Cortex-M SoC."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import struct
from typing import Any

import gdb
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .coresight import discover_rom_tables
from .cpuid import CPUID_ADDRESS, decode_cpuid
from .models import CPUID
from .providers import DEFAULT_PROVIDER_REGISTRY
from .target_memory import TargetMemoryReader, TargetReadError

CONSOLE = Console(force_terminal=True)

# --------------------------------------------------------------------------
# Architected register addresses (generic Arm-v6/v7/v8-M CMSIS core layout)
# --------------------------------------------------------------------------
SCB_VTOR = 0xE000ED08
SCB_CCR = 0xE000ED14
SCB_SHCSR = 0xE000ED24
SCB_CPACR = 0xE000ED88
SCB_NSACR = 0xE000ED8C

MPU_TYPE = 0xE000ED90
MPU_CTRL = 0xE000ED94
MPU_RNR = 0xE000ED98
MPU_RBAR = 0xE000ED9C
MPU_RASR_OR_RLAR = 0xE000EDA0

SAU_CTRL = 0xE000EDD0
SAU_TYPE = 0xE000EDD4
SAU_RNR = 0xE000EDD8
SAU_RBAR = 0xE000EDDC
SAU_RLAR = 0xE000EDE0

DHCSR = 0xE000EDF0

CCR_UNALIGN_TRP_BIT = 3
CCR_DIV_0_TRP_BIT = 4
CCR_BFHFNMIGN_BIT = 10

SHCSR_MEMFAULTENA_BIT = 16
SHCSR_BUSFAULTENA_BIT = 17
SHCSR_USGFAULTENA_BIT = 18
SHCSR_SECUREFAULTENA_BIT = 19

_V8M_CORES = ("Cortex-M23", "Cortex-M33", "Cortex-M35P", "Cortex-M52", "Cortex-M55", "Cortex-M85")
_BASELINE_CORES = ("Cortex-M0", "Cortex-M0+", "Cortex-M1")

# ARMv8.1-M cores that may optionally implement the PACBTI extension.
_V81M_CORES = ("Cortex-M52", "Cortex-M55", "Cortex-M85")

# Candidate GDB names for the Armv8.1-M privileged/unprivileged PAC key registers.
_PAC_KEY_P_REGISTERS = ("pac_key_p_0", "pac_key_p_1", "pac_key_p_2", "pac_key_p_3")
_PAC_KEY_U_REGISTERS = ("pac_key_u_0", "pac_key_u_1", "pac_key_u_2", "pac_key_u_3")

_VECTOR_HANDLERS: tuple[tuple[int, str, int | None], ...] = (
    (2, "NMI", None),
    (3, "HardFault", None),
    (4, "MemManage", SHCSR_MEMFAULTENA_BIT),
    (5, "BusFault", SHCSR_BUSFAULTENA_BIT),
    (6, "UsageFault", SHCSR_USGFAULTENA_BIT),
    (11, "SVCall", None),
    (14, "PendSV", None),
    (15, "SysTick", None),
)

# Best-effort STM32 Flash-option/RDP layouts, keyed by product-line prefix.
# (register address, byte shift of the RDP field within the 32-bit register)
_STM32_RDP_LAYOUTS: tuple[tuple[tuple[str, ...], int, int], ...] = (
    (("STM32F2", "STM32F4", "STM32F7"), 0x40023C14, 8),
    (("STM32H7",), 0x52002020, 8),
    (
        (
            "STM32F0",
            "STM32F1",
            "STM32F3",
            "STM32C0",
            "STM32G0",
            "STM32G4",
            "STM32L0",
            "STM32L1",
            "STM32L4",
            "STM32L5",
            "STM32U5",
            "STM32U0",
            "STM32U3",
            "STM32WB",
            "STM32WL",
        ),
        0x40022020,
        0,
    ),
)


class SecscanError(RuntimeError):
    """A fatal error that stops the security audit."""


@dataclass(frozen=True)
class SecscanFinding:
    """One security-audit observation."""

    category: str
    severity: str
    title: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        """Convert this finding to a plain dictionary.

        Returns
        -------
        dict[str, Any]
            Dictionary representation of the finding.
        """
        return {
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SecscanFinding:
        """Rebuild a finding from its dictionary representation.

        Parameters
        ----------
        data
            Dictionary previously produced by ``to_dict``.

        Returns
        -------
        SecscanFinding
            The restored finding.
        """
        return cls(
            category=str(data["category"]),
            severity=str(data["severity"]),
            title=str(data["title"]),
            detail=str(data["detail"]),
        )


@dataclass(frozen=True)
class SecscanReport:
    """A complete security-posture audit of one Cortex-M target."""

    core: str
    vendor: str | None
    device_name: str | None
    generated_at: str
    findings: tuple[SecscanFinding, ...]

    def counts(self) -> dict[str, int]:
        """Return the number of findings per severity level.

        Returns
        -------
        dict[str, int]
            Mapping of severity name to finding count.
        """
        counts = {"FAIL": 0, "WARN": 0, "INFO": 0, "PASS": 0}
        for finding in self.findings:
            counts[finding.severity] = counts.get(finding.severity, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        """Convert the complete report to a plain dictionary.

        Returns
        -------
        dict[str, Any]
            Dictionary representation of the report.
        """
        return {
            "core": self.core,
            "vendor": self.vendor,
            "device_name": self.device_name,
            "generated_at": self.generated_at,
            "summary": self.counts(),
            "findings": [f.to_dict() for f in self.findings],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SecscanReport:
        """Rebuild a report from its dictionary representation.

        Parameters
        ----------
        data
            Dictionary previously produced by ``to_dict``.

        Returns
        -------
        SecscanReport
            The restored report.
        """
        return cls(
            core=str(data["core"]),
            vendor=data.get("vendor"),
            device_name=data.get("device_name"),
            generated_at=str(data["generated_at"]),
            findings=tuple(SecscanFinding.from_dict(f) for f in data["findings"]),
        )


@dataclass
class _FindingCollector:
    """Accumulate findings emitted while running the successive audit passes."""

    findings: list[SecscanFinding] = field(default_factory=list)

    def add(self, category: str, severity: str, title: str, detail: str) -> None:
        """Record one finding.

        Parameters
        ----------
        category
            Audit section the finding belongs to.
        severity
            One of ``"PASS"``, ``"INFO"``, ``"WARN"``, ``"FAIL"``.
        title
            Short finding summary.
        detail
            Longer explanation of the finding.
        """
        self.findings.append(SecscanFinding(category, severity, title, detail))


def _is_armv8m(cpuid: CPUID) -> bool:
    """Return whether the decoded core implements the ARMv8-M architecture."""
    return cpuid.core in _V8M_CORES


def _has_configurable_faults(cpuid: CPUID) -> bool:
    """Return whether the core implements MemManage/BusFault/UsageFault handlers."""
    return cpuid.core not in _BASELINE_CORES


def _read_register_any(frame: gdb.Frame, names: tuple[str, ...]) -> int | None:
    """Read the first available named core register.

    Parameters
    ----------
    frame
        The selected GDB frame.
    names
        Candidate register names, tried in order.

    Returns
    -------
    int | None
        The register value, or ``None`` if no candidate name is available.
    """
    for name in names:
        try:
            return int(frame.read_register(name))
        except (gdb.error, ValueError, TypeError):
            continue
    return None


def _write_uint32(address: int, value: int) -> None:
    """Write a little-endian unsigned 32-bit value to target memory.

    Raises
    ------
    SecscanError
        If the target rejects the write.
    """
    try:
        gdb.selected_inferior().write_memory(address, struct.pack("<I", value & 0xFFFFFFFF))
    except (gdb.error, gdb.MemoryError) as error:
        raise SecscanError(f"could not write 0x{value:08X} at 0x{address:08X}: {error}") from error


@dataclass(frozen=True)
class MpuRegionInfo:
    """One decoded MPU region, normalized across ARMv6/7-M and ARMv8-M layouts."""

    index: int
    enabled: bool
    base: int
    limit: int
    writable: bool
    executable: bool


def _decode_mpu_region_v8m(rbar: int, rlar: int, index: int) -> MpuRegionInfo:
    """Decode one ARMv8-M MPU region from its RBAR/RLAR pair."""
    base = rbar & 0xFFFFFFE0
    ap = (rbar >> 1) & 0x3
    xn = rbar & 0x1
    limit = (rlar & 0xFFFFFFE0) | 0x1F
    enabled = bool(rlar & 0x1)
    writable = ap in (0b00, 0b01)
    return MpuRegionInfo(index, enabled, base, limit, writable, xn == 0)


def _decode_mpu_region_v7m(rbar: int, rasr: int, index: int) -> MpuRegionInfo:
    """Decode one ARMv6/7-M MPU region from its RBAR/RASR pair."""
    base = rbar & 0xFFFFFFE0
    enabled = bool(rasr & 0x1)
    size_field = (rasr >> 1) & 0x1F
    size_bytes = 1 << (size_field + 1)
    ap = (rasr >> 24) & 0x7
    xn = (rasr >> 28) & 0x1
    limit = base + size_bytes - 1
    writable = ap in (0b001, 0b010, 0b011)
    return MpuRegionInfo(index, enabled, base, limit, writable, xn == 0)


def _overlaps(a: MpuRegionInfo, b: MpuRegionInfo) -> bool:
    """Return whether two enabled regions cover a common address."""
    return a.base <= b.limit and b.base <= a.limit


def _audit_mpu(
    reader: TargetMemoryReader,
    frame: gdb.Frame,
    cpuid: CPUID,
    collector: _FindingCollector,
) -> None:
    """Audit MPU presence, region validity, W^X, and stack/RAM/Flash protections."""
    category = "MPU"
    armv8m = _is_armv8m(cpuid)

    try:
        mpu_type = reader.read_uint32(MPU_TYPE)
    except TargetReadError as error:
        collector.add(category, "INFO", "MPU not accessible", f"Cannot read MPU_TYPE: {error}")
        return

    dregion = (mpu_type >> 8) & 0xFF
    if dregion == 0:
        collector.add(category, "INFO", "MPU not implemented", "MPU_TYPE.DREGION is 0.")
        return

    mpu_ctrl = reader.read_uint32(MPU_CTRL)
    mpu_enabled = bool(mpu_ctrl & 0x1)
    privdefena = bool(mpu_ctrl & 0x4)

    if not mpu_enabled:
        collector.add(
            category,
            "FAIL",
            "MPU is disabled",
            f"MPU_CTRL.ENABLE=0 while {dregion} region(s) are implemented; no memory protection is active.",
        )
    else:
        collector.add(category, "PASS", "MPU is enabled", f"{dregion} region(s) implemented.")

    original_rnr = reader.read_uint32(MPU_RNR)
    regions: list[MpuRegionInfo] = []
    try:
        for index in range(dregion):
            try:
                _write_uint32(MPU_RNR, index)
                rbar = reader.read_uint32(MPU_RBAR)
                second = reader.read_uint32(MPU_RASR_OR_RLAR)
            except (SecscanError, TargetReadError) as error:
                collector.add(
                    category,
                    "INFO",
                    f"Region {index} unreadable",
                    str(error),
                )
                continue
            if armv8m:
                regions.append(_decode_mpu_region_v8m(rbar, second, index))
            else:
                regions.append(_decode_mpu_region_v7m(rbar, second, index))
    finally:
        try:
            _write_uint32(MPU_RNR, original_rnr)
        except SecscanError:
            pass

    enabled_regions = [r for r in regions if r.enabled]

    for region in enabled_regions:
        if region.limit <= region.base:
            collector.add(
                category,
                "WARN",
                f"Region {region.index} has an invalid size",
                f"base=0x{region.base:08X} limit=0x{region.limit:08X}.",
            )
        if region.writable and region.executable:
            collector.add(
                category,
                "FAIL",
                f"Region {region.index} violates W^X",
                f"0x{region.base:08X}-0x{region.limit:08X} is both writable and executable.",
            )

    sorted_regions = sorted(enabled_regions, key=lambda r: r.base)
    for prev, cur in zip(sorted_regions, sorted_regions[1:]):
        if _overlaps(prev, cur):
            collector.add(
                category,
                "FAIL",
                f"Regions {prev.index} and {cur.index} overlap",
                f"0x{prev.base:08X}-0x{prev.limit:08X} overlaps 0x{cur.base:08X}-0x{cur.limit:08X}.",
            )

    if mpu_enabled:
        for sp_name, reg_names in (
            ("MSP", ("msp", "msp_ns", "msp_s")),
            ("PSP", ("psp", "psp_ns", "psp_s")),
        ):
            sp_val = _read_register_any(frame, reg_names)
            if sp_val is None:
                continue
            covering = next((r for r in enabled_regions if r.base <= sp_val <= r.limit), None)
            if covering is None:
                if privdefena:
                    collector.add(
                        category,
                        "WARN",
                        f"{sp_name} falls back to the default background map",
                        f"0x{sp_val:08X} is not covered by any explicit region; "
                        "MPU_CTRL.PRIVDEFENA=1 grants the default executable memory map.",
                    )
                else:
                    collector.add(
                        category,
                        "FAIL",
                        f"{sp_name} is not covered by any MPU region",
                        f"0x{sp_val:08X} has no matching region and PRIVDEFENA=0 (access denied).",
                    )
            elif covering.executable:
                collector.add(
                    category,
                    "FAIL",
                    f"{sp_name} stack region is executable",
                    f"Region {covering.index} covering 0x{sp_val:08X} does not set XN.",
                )
            else:
                collector.add(
                    category,
                    "PASS",
                    f"{sp_name} stack region is non-executable",
                    f"Region {covering.index} covers 0x{sp_val:08X} with XN set.",
                )

    ram_start, ram_end = 0x20000000, 0x40000000
    flash_start, flash_end = 0x00000000, 0x20000000
    for region in enabled_regions:
        if region.base < ram_end and region.limit >= ram_start:
            if region.executable:
                collector.add(
                    category,
                    "FAIL",
                    f"Region {region.index} makes RAM executable",
                    f"0x{region.base:08X}-0x{region.limit:08X} overlaps SRAM and is not XN.",
                )
        if region.base < flash_end and region.limit >= flash_start:
            if region.writable:
                collector.add(
                    category,
                    "FAIL",
                    f"Region {region.index} makes Flash writable",
                    f"0x{region.base:08X}-0x{region.limit:08X} overlaps Flash/code and is writable.",
                )
            if not region.executable:
                collector.add(
                    category,
                    "WARN",
                    f"Region {region.index} makes Flash non-executable",
                    f"0x{region.base:08X}-0x{region.limit:08X} overlaps Flash/code and sets XN.",
                )


def _audit_cmsis_core(
    reader: TargetMemoryReader, cpuid: CPUID, collector: _FindingCollector
) -> None:
    """Audit generic CMSIS core security configuration (SHCSR, CCR, DHCSR)."""
    category = "CMSIS Core Security"

    if _has_configurable_faults(cpuid):
        shcsr = reader.read_uint32(SCB_SHCSR)
        missing = [
            name
            for name, bit in (
                ("MemManage", SHCSR_MEMFAULTENA_BIT),
                ("BusFault", SHCSR_BUSFAULTENA_BIT),
                ("UsageFault", SHCSR_USGFAULTENA_BIT),
            )
            if not (shcsr & (1 << bit))
        ]
        if missing:
            collector.add(
                category,
                "WARN",
                "Configurable fault handlers disabled",
                f"{', '.join(missing)} disabled in SHCSR; these faults will escalate to HardFault.",
            )
        else:
            collector.add(
                category,
                "PASS",
                "Configurable fault handlers enabled",
                "MemManage, BusFault, and UsageFault are all enabled in SHCSR.",
            )
    else:
        collector.add(
            category,
            "INFO",
            "No configurable fault handlers on this core",
            f"{cpuid.core} only implements HardFault.",
        )

    ccr = reader.read_uint32(SCB_CCR)
    if not (ccr & (1 << CCR_DIV_0_TRP_BIT)):
        collector.add(
            category,
            "WARN",
            "Division-by-zero trap disabled",
            "CCR.DIV_0_TRP=0: integer division by zero silently returns 0 instead of faulting.",
        )
    if not (ccr & (1 << CCR_UNALIGN_TRP_BIT)):
        collector.add(
            category,
            "INFO",
            "Unaligned-access trap disabled",
            "CCR.UNALIGN_TRP=0: unaligned accesses are silently allowed except for LDM/STM/PUSH/POP.",
        )
    if ccr & (1 << CCR_BFHFNMIGN_BIT):
        collector.add(
            category,
            "WARN",
            "BusFault ignored at high priority",
            "CCR.BFHFNMIGN=1: precise data-bus faults are ignored in HardFault/NMI handlers "
            "and priority -1/-2 code.",
        )

    try:
        dhcsr = reader.read_uint32(DHCSR)
    except TargetReadError as error:
        collector.add(category, "INFO", "DHCSR unavailable", str(error))
    else:
        if dhcsr & 0x1:
            collector.add(
                category,
                "INFO",
                "Debug access is currently enabled",
                "DHCSR.C_DEBUGEN=1: ensure the debug port is disabled/locked in production.",
            )
        else:
            collector.add(category, "PASS", "Debug access is disabled", "DHCSR.C_DEBUGEN=0.")


def _audit_trustzone(reader: TargetMemoryReader, collector: _FindingCollector) -> None:
    """Audit TrustZone-M (SAU) presence, activation, and region configuration."""
    category = "TrustZone (SAU)"

    try:
        sau_type = reader.read_uint32(SAU_TYPE)
    except TargetReadError:
        collector.add(
            category,
            "INFO",
            "TrustZone-M not implemented",
            "SAU_TYPE is not accessible on this core.",
        )
        return

    sregion = sau_type & 0xFF
    sau_ctrl = reader.read_uint32(SAU_CTRL)
    sau_enabled = bool(sau_ctrl & 0x1)
    allns = bool(sau_ctrl & 0x2)

    if not sau_enabled:
        if sregion == 0:
            collector.add(
                category,
                "INFO",
                "TrustZone-M not used",
                "SAU implements 0 regions and is disabled; this is likely a non-secure-only build.",
            )
        else:
            collector.add(
                category,
                "WARN",
                "SAU implemented but disabled",
                f"SAU_CTRL.ENABLE=0 with {sregion} region(s) available; "
                "all memory is treated as Non-Secure and TrustZone is not enforced.",
            )
        return

    if allns:
        collector.add(
            category,
            "WARN",
            "SAU_CTRL.ALLNS is set",
            "All memory defaults to Non-Secure outside matched regions; verify region coverage.",
        )

    original_rnr = reader.read_uint32(SAU_RNR)
    sau_regions: list[MpuRegionInfo] = []
    try:
        for index in range(sregion):
            try:
                _write_uint32(SAU_RNR, index)
                rbar = reader.read_uint32(SAU_RBAR)
                rlar = reader.read_uint32(SAU_RLAR)
            except (SecscanError, TargetReadError) as error:
                collector.add(category, "INFO", f"SAU region {index} unreadable", str(error))
                continue
            base = rbar & 0xFFFFFFE0
            limit = (rlar & 0xFFFFFFE0) | 0x1F
            enabled = bool(rlar & 0x1)
            sau_regions.append(MpuRegionInfo(index, enabled, base, limit, False, False))
    finally:
        try:
            _write_uint32(SAU_RNR, original_rnr)
        except SecscanError:
            pass

    enabled_regions = [r for r in sau_regions if r.enabled]
    if not enabled_regions:
        collector.add(
            category,
            "WARN",
            "SAU is enabled without active regions",
            f"SAU_CTRL.ENABLE=1 but none of the {sregion} region(s) are enabled.",
        )
    else:
        collector.add(
            category,
            "PASS",
            "TrustZone-M active",
            f"{len(enabled_regions)} enabled Secure-attribution region(s) out of {sregion}.",
        )

    sorted_regions = sorted(enabled_regions, key=lambda r: r.base)
    for prev, cur in zip(sorted_regions, sorted_regions[1:]):
        if _overlaps(prev, cur):
            collector.add(
                category,
                "WARN",
                f"SAU regions {prev.index} and {cur.index} overlap",
                f"0x{prev.base:08X}-0x{prev.limit:08X} overlaps 0x{cur.base:08X}-0x{cur.limit:08X}.",
            )


def _audit_vtor(reader: TargetMemoryReader, cpuid: CPUID, collector: _FindingCollector) -> None:
    """Audit the vector table via VTOR and check critical fault handlers exist."""
    category = "Fault Handlers (VTOR)"
    vtor = reader.read_uint32(SCB_VTOR)
    collector.add(category, "INFO", "Vector table base", f"VTOR=0x{vtor:08X}.")

    has_config_faults = _has_configurable_faults(cpuid)
    shcsr = reader.read_uint32(SCB_SHCSR)

    for index, name, enable_bit in _VECTOR_HANDLERS:
        if enable_bit in (SHCSR_MEMFAULTENA_BIT, SHCSR_BUSFAULTENA_BIT, SHCSR_USGFAULTENA_BIT):
            if not has_config_faults:
                continue
        try:
            handler = reader.read_uint32(vtor + 4 * index)
        except TargetReadError as error:
            collector.add(category, "INFO", f"{name} handler unreadable", str(error))
            continue

        if handler in (0x00000000, 0xFFFFFFFF):
            collector.add(
                category,
                "FAIL",
                f"{name} handler is missing",
                f"Vector[{index}]=0x{handler:08X}: the core will lock up on this exception.",
            )
            continue

        if not handler & 0x1:
            collector.add(
                category,
                "WARN",
                f"{name} handler is not Thumb-encoded",
                f"Vector[{index}]=0x{handler:08X}: bit0 is clear, entry will UsageFault.",
            )
        else:
            collector.add(category, "PASS", f"{name} handler is present", f"0x{handler:08X}.")

        if enable_bit is not None and not (shcsr & (1 << enable_bit)):
            collector.add(
                category,
                "WARN",
                f"{name} handler configured but not enabled",
                f"SHCSR bit {enable_bit} is clear; faults will escalate to HardFault instead.",
            )


def _audit_stack_limits(frame: gdb.Frame, cpuid: CPUID, collector: _FindingCollector) -> None:
    """Audit the ARMv8-M MSPLIM/PSPLIM stack-limit registers."""
    if not _is_armv8m(cpuid):
        return

    category = "ARMv8-M Stack Limits"

    for sp_name, sp_names, lim_names in (
        ("MSP", ("msp", "msp_ns", "msp_s"), ("msplim", "msplim_ns", "msplim_s")),
        ("PSP", ("psp", "psp_ns", "psp_s"), ("psplim", "psplim_ns", "psplim_s")),
    ):
        limit = _read_register_any(frame, lim_names)
        if limit is None:
            collector.add(
                category,
                "INFO",
                f"{sp_name}LIM unavailable",
                "GDB does not expose this register for the current target description.",
            )
            continue

        if limit == 0:
            collector.add(
                category,
                "WARN",
                f"{sp_name}LIM is not configured",
                f"{sp_name}LIM=0x00000000: stack-overflow hardware detection is disabled for {sp_name}.",
            )
        else:
            collector.add(
                category,
                "PASS",
                f"{sp_name}LIM is configured",
                f"{sp_name}LIM=0x{limit:08X}.",
            )

        sp_val = _read_register_any(frame, sp_names)
        if sp_val is not None and limit is not None and limit != 0 and sp_val < limit:
            collector.add(
                category,
                "FAIL",
                f"{sp_name} is below its configured limit",
                f"{sp_name}=0x{sp_val:08X} < {sp_name}LIM=0x{limit:08X}: stack already overflowed.",
            )


def _audit_pacbti(frame: gdb.Frame, cpuid: CPUID, collector: _FindingCollector) -> None:
    """Audit ARMv8.1-M Pointer Authentication (PAC) and Branch Target Identification (BTI).

    PAC/BTI have no runtime enable bit: BTI landing pads are always active when the
    extension is implemented, and PAC strength only depends on the key material and on
    the firmware being compiled with return-address signing enabled. This check is
    therefore best-effort: it only reports what can be observed through the optional
    PAC key registers exposed by GDB's target description.
    """
    if cpuid.core not in _V81M_CORES:
        return

    category = "PACBTI (ARMv8.1-M)"

    key_p_words = [_read_register_any(frame, (name,)) for name in _PAC_KEY_P_REGISTERS]
    if any(word is None for word in key_p_words):
        collector.add(
            category,
            "INFO",
            "PAC/BTI status cannot be determined",
            f"{cpuid.core} may implement the optional Armv8.1-M PACBTI extension, but GDB "
            "does not expose the PAC_KEY_P_* registers for this target; the extension may "
            "not be implemented, or the security state/target description hides it.",
        )
        return

    if all(word == 0 for word in key_p_words):
        collector.add(
            category,
            "WARN",
            "PAC privileged key is all-zero",
            "PAC_KEY_P_0..3 are all 0: return-address signing provides no real protection "
            "until firmware provisions a random, non-predictable key.",
        )
    else:
        collector.add(
            category,
            "PASS",
            "PAC privileged key material is present",
            "PAC_KEY_P_0..3 are populated; PAC is available for firmware compiled with "
            "return-address signing (-mbranch-protection=pac-ret).",
        )

    collector.add(
        category,
        "INFO",
        "BTI has no runtime enable bit",
        "Branch Target Identification is always active for code compiled with BTI landing "
        "pads on cores implementing the extension; verify the firmware was built with "
        "-mbranch-protection=bti (or pac-ret+bti) to benefit from it.",
    )


def _decode_stm32_rdp(
    reader: TargetMemoryReader, product_line: str, collector: _FindingCollector
) -> None:
    """Decode the STM32 Readout Protection (RDP) level for a recognized product line."""
    category = "STM32 Readout Protection (RDP)"
    normalized = product_line.upper()

    layout = next(
        (
            (address, shift)
            for prefixes, address, shift in _STM32_RDP_LAYOUTS
            if any(prefix in normalized for prefix in prefixes)
        ),
        None,
    )
    if layout is None:
        collector.add(
            category,
            "INFO",
            "RDP layout not documented",
            f"No known Flash-option register layout for product line '{product_line}'.",
        )
        return

    address, shift = layout
    try:
        raw = reader.read_uint32(address)
    except TargetReadError as error:
        collector.add(category, "INFO", "RDP register unreadable", str(error))
        return

    rdp_byte = (raw >> shift) & 0xFF
    if rdp_byte == 0xAA:
        collector.add(
            category, "WARN", "RDP level 0 (no protection)", f"0x{address:08X}=0x{raw:08X}."
        )
    elif rdp_byte == 0xCC:
        collector.add(
            category,
            "PASS",
            "RDP level 2 (fully protected, debug disabled)",
            f"0x{address:08X}=0x{raw:08X}.",
        )
    else:
        collector.add(
            category,
            "PASS",
            "RDP level 1 (debug restricted)",
            f"0x{address:08X}=0x{raw:08X}, RDP byte=0x{rdp_byte:02X}.",
        )


def run_audit() -> SecscanReport:
    """Run the complete security audit against the currently selected target.

    Returns
    -------
    SecscanReport
        The full audit report.

    Raises
    ------
    gdb.GdbError
        If the target CPUID cannot be read or the architecture is unsupported.
    """
    frame = gdb.selected_frame()
    architecture_name = frame.architecture().name().lower()
    if not any(k in architecture_name for k in ("arm", "cortex-m", "thumb")):
        raise gdb.GdbError("secscan only supports ARM Cortex-M targets")

    try:
        reader = TargetMemoryReader()
        cpuid = decode_cpuid(reader.read_uint32(CPUID_ADDRESS))
    except TargetReadError as error:
        raise gdb.GdbError(str(error)) from error

    discovery = discover_rom_tables(reader)
    device_report = DEFAULT_PROVIDER_REGISTRY.inspect(reader, cpuid, discovery)

    collector = _FindingCollector()
    _audit_mpu(reader, frame, cpuid, collector)
    _audit_cmsis_core(reader, cpuid, collector)
    _audit_trustzone(reader, collector)
    _audit_vtor(reader, cpuid, collector)
    _audit_stack_limits(frame, cpuid, collector)
    _audit_pacbti(frame, cpuid, collector)

    if device_report.vendor == "STMicroelectronics":
        product_line = device_report.product_line.display()
        _decode_stm32_rdp(reader, product_line, collector)

    device_name = (
        device_report.product_line.display() if device_report.product_line.is_available else None
    )
    return SecscanReport(
        core=cpuid.core,
        vendor=device_report.vendor,
        device_name=device_name,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        findings=tuple(collector.findings),
    )


_SEVERITY_STYLE = {
    "FAIL": "bold red",
    "WARN": "bold yellow",
    "INFO": "cyan",
    "PASS": "bold green",
}


def render_report(report: SecscanReport) -> None:
    """Render a security-audit report through the shared Rich console.

    Parameters
    ----------
    report
        The audit report to display.
    """
    counts = report.counts()
    summary = (
        f"Core: {report.core}  |  Vendor: {report.vendor or 'Unknown'}  |  "
        f"Device: {report.device_name or 'Unknown'}\n"
        f"Generated: {report.generated_at}\n"
        f"[bold red]FAIL: {counts['FAIL']}[/bold red]  "
        f"[bold yellow]WARN: {counts['WARN']}[/bold yellow]  "
        f"[cyan]INFO: {counts['INFO']}[/cyan]  "
        f"[bold green]PASS: {counts['PASS']}[/bold green]"
    )
    CONSOLE.print(Panel(summary, title="secscan audit summary", box=box.SIMPLE_HEAVY))

    categories: list[str] = []
    for finding in report.findings:
        if finding.category not in categories:
            categories.append(finding.category)

    for category in categories:
        table = Table(
            title=category,
            box=box.SIMPLE_HEAVY,
            header_style="bold cyan",
            show_header=True,
        )
        table.add_column("Severity", no_wrap=True)
        table.add_column("Finding", style="bold")
        table.add_column("Detail")
        for finding in report.findings:
            if finding.category != category:
                continue
            style = _SEVERITY_STYLE.get(finding.severity, "")
            table.add_row(Text(finding.severity, style=style), finding.title, finding.detail)
        CONSOLE.print(table)


def dump_report_to_json(report: SecscanReport, output_path: Path) -> int:
    """Serialize an audit report to a JSON file on disk.

    Parameters
    ----------
    report
        The audit report to serialize.
    output_path
        Destination file path.

    Returns
    -------
    int
        Size of the written file in bytes.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as json_file:
        json.dump(report.to_dict(), json_file, indent=2)
    return output_path.stat().st_size


_HTML_STYLE = """
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; color: #1a1a1a; }
  h1 { margin-bottom: 0.25rem; }
  .meta { color: #555; margin-bottom: 1.5rem; }
  .summary { display: flex; gap: 1rem; margin-bottom: 2rem; }
  .badge { padding: 0.5rem 1rem; border-radius: 6px; font-weight: 600; color: #fff; }
  .badge.fail { background: #c0392b; }
  .badge.warn { background: #b9770e; }
  .badge.info { background: #2874a6; }
  .badge.pass { background: #1e8449; }
  section { margin-bottom: 2rem; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border: 1px solid #ddd; padding: 0.5rem 0.75rem; text-align: left; vertical-align: top; }
  th { background: #f4f4f4; }
  td.sev { font-weight: 700; white-space: nowrap; }
  tr.sev-fail td.sev { color: #c0392b; }
  tr.sev-warn td.sev { color: #b9770e; }
  tr.sev-info td.sev { color: #2874a6; }
  tr.sev-pass td.sev { color: #1e8449; }
"""

_HTML_SEVERITY_CLASS = {
    "FAIL": "sev-fail",
    "WARN": "sev-warn",
    "INFO": "sev-info",
    "PASS": "sev-pass",
}


def _html_finding_row(finding: SecscanFinding) -> str:
    """Render one finding as an HTML table row, escaping all user-facing text."""
    row_class = _HTML_SEVERITY_CLASS.get(finding.severity, "")
    severity = html.escape(finding.severity)
    title = html.escape(finding.title)
    detail = html.escape(finding.detail)
    return (
        f'<tr class="{row_class}"><td class="sev">{severity}</td>'
        f"<td>{title}</td><td>{detail}</td></tr>"
    )


def generate_html_report(report: SecscanReport) -> str:
    """Render a security-audit report as a standalone HTML document.

    Parameters
    ----------
    report
        The audit report to render.

    Returns
    -------
    str
        A self-contained HTML document (inline CSS, no external assets).
    """
    counts = report.counts()

    categories: list[str] = []
    for finding in report.findings:
        if finding.category not in categories:
            categories.append(finding.category)

    sections: list[str] = []
    for category in categories:
        rows = "\n".join(
            _html_finding_row(finding)
            for finding in report.findings
            if finding.category == category
        )
        sections.append(
            f"<section><h2>{html.escape(category)}</h2>"
            "<table><thead><tr><th>Severity</th><th>Finding</th><th>Detail</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></section>"
        )

    head = (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f"<title>secscan report - {html.escape(report.core)}</title>\n"
        f"<style>{_HTML_STYLE}</style>\n</head>\n<body>\n"
    )
    meta = (
        "<h1>secscan audit report</h1>\n"
        f'<p class="meta">Core: {html.escape(report.core)} &middot; '
        f"Vendor: {html.escape(report.vendor or 'Unknown')} &middot; "
        f"Device: {html.escape(report.device_name or 'Unknown')} &middot; "
        f"Generated: {html.escape(report.generated_at)}</p>\n"
    )
    summary = (
        '<div class="summary">'
        f'<span class="badge fail">FAIL {counts["FAIL"]}</span>'
        f'<span class="badge warn">WARN {counts["WARN"]}</span>'
        f'<span class="badge info">INFO {counts["INFO"]}</span>'
        f'<span class="badge pass">PASS {counts["PASS"]}</span>'
        "</div>\n"
    )
    return head + meta + summary + "\n".join(sections) + "\n</body>\n</html>\n"


def dump_report_to_html(report: SecscanReport, output_path: Path) -> int:
    """Write a standalone HTML report to disk.

    Parameters
    ----------
    report
        The audit report to render.
    output_path
        Destination file path.

    Returns
    -------
    int
        Size of the written file in bytes.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as html_file:
        html_file.write(generate_html_report(report))
    return output_path.stat().st_size


def load_report_from_json(input_path: Path) -> SecscanReport:
    """Load a previously dumped audit report from a JSON file.

    Parameters
    ----------
    input_path
        Source file path.

    Returns
    -------
    SecscanReport
        The restored audit report.

    Raises
    ------
    OSError
        If the file cannot be read.
    ValueError
        If the file content is not a valid audit report.
    """
    with input_path.open("r", encoding="utf-8") as json_file:
        data = json.load(json_file)
    try:
        return SecscanReport.from_dict(data)
    except (KeyError, TypeError) as error:
        raise ValueError(f"'{input_path}' is not a valid secscan report: {error}") from error


def render_help() -> None:
    """Render the secscan command overview and help table."""
    table = Table(
        title="Cortex-M Security Posture Audit",
        box=box.SIMPLE_HEAVY,
        header_style="bold cyan",
        show_header=True,
    )
    table.add_column("Command Syntax", style="bold yellow", no_wrap=True)
    table.add_column("Description")
    table.add_row(
        "secscan audit [<output.json>]",
        "Run the full security audit against the live target and optionally dump it to JSON",
    )
    table.add_row(
        "secscan report <report.json> [--html <output.html>]",
        "Render a clean report from a previously dumped audit file (console by default, "
        "or a standalone HTML file with --html)",
    )
    table.add_row("secscan help", "Show this command reference")
    CONSOLE.print(table)


class SecscanCmd(gdb.Command):
    """Audit the security posture of an ARM Cortex-M target."""

    def __init__(self) -> None:
        """Register the command with GDB."""
        super().__init__("secscan", gdb.COMMAND_USER, gdb.COMPLETE_NONE, True)

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Display help overview.

        Parameters
        ----------
        arg
            Command arguments.
        from_tty
            Whether GDB invoked the command from its terminal.
        """
        del arg
        del from_tty
        render_help()

    def _invoke_audit(self, args: list[str]) -> None:
        """Handle 'secscan audit [<output.json>]'.

        Parameters
        ----------
        args
            Subcommand arguments.

        Raises
        ------
        gdb.GdbError
            If arguments are invalid or writing the optional dump fails.
        """
        if len(args) > 1:
            raise gdb.GdbError("Usage: secscan audit [<output.json>]")

        report = run_audit()
        render_report(report)

        if args:
            out_path = Path(args[0]).expanduser().resolve()
            try:
                dump_report_to_json(report, out_path)
            except OSError as error:
                raise gdb.GdbError(f"Failed to write dump to '{out_path}': {error}") from error
            CONSOLE.print(f"[green]Report written to {out_path}[/green]")

    def _invoke_report(self, args: list[str]) -> None:
        """Handle 'secscan report <report.json> [--html <output.html>]'.

        Parameters
        ----------
        args
            Subcommand arguments.

        Raises
        ------
        gdb.GdbError
            If arguments are invalid or the file cannot be read/written.
        """
        usage = "Usage: secscan report <report.json> [--html <output.html>]"

        html_arg: str | None = None
        positional: list[str] = []
        index = 0
        while index < len(args):
            token = args[index]
            if token == "--html":
                if index + 1 >= len(args):
                    raise gdb.GdbError(f"--html requires an output file path. {usage}")
                html_arg = args[index + 1]
                index += 2
                continue
            positional.append(token)
            index += 1

        if len(positional) != 1:
            raise gdb.GdbError(usage)

        input_path = Path(positional[0]).expanduser().resolve()
        try:
            report = load_report_from_json(input_path)
        except (OSError, ValueError) as error:
            raise gdb.GdbError(f"Failed to read report from '{input_path}': {error}") from error

        if html_arg is not None:
            html_path = Path(html_arg).expanduser().resolve()
            try:
                file_size = dump_report_to_html(report, html_path)
            except OSError as error:
                raise gdb.GdbError(
                    f"Failed to write HTML report to '{html_path}': {error}"
                ) from error
            CONSOLE.print(f"[green]HTML report written to {html_path} ({file_size} bytes)[/green]")
            return

        render_report(report)


class _SecscanSubcommand(gdb.Command):
    """Base class for concrete ``secscan`` subcommands registered under the prefix command."""

    def __init__(self, parent: SecscanCmd, name: str) -> None:
        """Register one ``secscan <name>`` subcommand.

        Parameters
        ----------
        parent
            The prefix command instance.
        name
            Subcommand name.
        """
        self.parent = parent
        self.name = name
        super().__init__(f"secscan {name}", gdb.COMMAND_USER)

    def _argv(self, arg: str) -> list[str]:
        """Split command arguments using GDB's CLI lexer.

        Parameters
        ----------
        arg
            Argument string.

        Returns
        -------
        list[str]
            Parsed arguments.
        """
        return list(gdb.string_to_argv(arg)) if arg.strip() else []


class SecscanAuditCmd(_SecscanSubcommand):
    """Run the full security audit against the live target."""

    def __init__(self, parent: SecscanCmd) -> None:
        """Register the ``secscan audit`` subcommand."""
        super().__init__(parent, "audit")

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Execute ``secscan audit [<output.json>]``."""
        del from_tty
        self.parent._invoke_audit(self._argv(arg))


class SecscanReportCmd(_SecscanSubcommand):
    """Render a previously dumped audit report from a JSON file."""

    def __init__(self, parent: SecscanCmd) -> None:
        """Register the ``secscan report`` subcommand."""
        super().__init__(parent, "report")

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Execute ``secscan report <report.json>``."""
        del from_tty
        self.parent._invoke_report(self._argv(arg))


class SecscanHelpCmd(_SecscanSubcommand):
    """Show the secscan command reference."""

    def __init__(self, parent: SecscanCmd) -> None:
        """Register the ``secscan help`` subcommand."""
        super().__init__(parent, "help")

    def invoke(self, arg: str, from_tty: bool) -> None:
        """Execute ``secscan help``."""
        del arg
        del from_tty
        render_help()
