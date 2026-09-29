# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Arm Cortex-M security-posture diagnostic service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import gdb

from ...target_memory import TargetMemory, TargetReadError, WritableTargetMemory
from ..base import Architecture, SystemRegisterSet, TargetDescription
from ..diagnostics import (
    DiagnosticField,
    DiagnosticFinding,
    DiagnosticReport,
    DiagnosticRuntimeAccess,
    DiagnosticSection,
    DiagnosticServiceName,
    DiagnosticSeverity,
)
from .coresight import discover_rom_tables
from .cortex_m import CortexMArchitecture, CortexMFeature, CortexMTargetDescription, read_scb
from .mpu import MpuAccessPermission, MpuDump, MpuRegion, dump_mpu_regions
from .providers import DEFAULT_PROVIDER_REGISTRY
from .sau import dump_sau_regions, read_sau_status

_DHCSR_ADDRESS = 0xE000EDF0
_CCR_UNALIGN_TRP_BIT = 3
_CCR_DIV_0_TRP_BIT = 4
_CCR_BFHFNMIGN_BIT = 10
_SHCSR_MEMFAULTENA_BIT = 16
_SHCSR_BUSFAULTENA_BIT = 17
_SHCSR_USGFAULTENA_BIT = 18
_V81M_ARCHITECTURES = frozenset((CortexMArchitecture.ARMV8_1_M_MAINLINE,))
_PAC_KEY_P_REGISTERS = ("pac_key_p_0", "pac_key_p_1", "pac_key_p_2", "pac_key_p_3")
_VECTOR_HANDLERS: tuple[tuple[int, str, int | None], ...] = (
    (2, "NMI", None),
    (3, "HardFault", None),
    (4, "MemManage", _SHCSR_MEMFAULTENA_BIT),
    (5, "BusFault", _SHCSR_BUSFAULTENA_BIT),
    (6, "UsageFault", _SHCSR_USGFAULTENA_BIT),
    (11, "SVCall", None),
    (14, "PendSV", None),
    (15, "SysTick", None),
)
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


class CortexMRegisterReader(Protocol):
    """Read an exposed Cortex-M architectural register by one of several names."""

    def read_first(self, names: tuple[str, ...]) -> int | None:
        """Return the first exposed register value, or ``None`` when none are exposed."""
        ...


class GdbCortexMRegisterReader:
    """Read Cortex-M core registers from GDB's selected frame."""

    def read_first(self, names: tuple[str, ...]) -> int | None:
        """Return the first GDB register exposed under a candidate name."""
        frame = gdb.selected_frame()
        for name in names:
            try:
                return int(frame.read_register(name))
            except (gdb.error, ValueError, TypeError):
                continue
        return None


@dataclass
class _FindingCollector:
    """Collect portable findings while retaining the established severity vocabulary."""

    findings: list[DiagnosticFinding]

    def __init__(self) -> None:
        """Create an empty finding collection."""
        self.findings = []

    def add(self, category: str, severity: DiagnosticSeverity, title: str, detail: str) -> None:
        """Append one portable security finding."""
        self.findings.append(DiagnosticFinding(category, severity, title, detail))


class CortexMSecurityAuditor:
    """Collect the Arm-specific implementation of the portable security audit."""

    architecture = Architecture.ARM
    service = DiagnosticServiceName.SECURITY_AUDIT

    def __init__(self, registers: CortexMRegisterReader | None = None) -> None:
        """Create an auditor using GDB registers unless a test adapter is supplied."""
        self._registers = registers if registers is not None else GdbCortexMRegisterReader()

    def supports(self, target: TargetDescription) -> bool:
        """Return whether the target is a known Cortex-M implementation."""
        return isinstance(target, CortexMTargetDescription) and target.core is not None

    def collect(
        self,
        reader: TargetMemory,
        target: TargetDescription,
        access: DiagnosticRuntimeAccess | None = None,
    ) -> DiagnosticReport:
        """Collect Cortex-M security controls and device-specific protections."""
        if not isinstance(target, CortexMTargetDescription) or target.core is None:
            raise ValueError("Cortex-M security auditing requires a known core description")

        scb = read_scb(reader, target, cpuid_value=target.raw_cpuid)
        discovery = discover_rom_tables(reader)
        device_report = DEFAULT_PROVIDER_REGISTRY.inspect(reader, target, discovery)
        collector = _FindingCollector()

        registers = (
            access.registers
            if access is not None and access.registers is not None
            else self._registers
        )

        self._audit_mpu(reader, target, collector, registers)
        self._audit_cmsis_core(reader, target, scb, collector)
        self._audit_trustzone(reader, target, collector)
        self._audit_vtor(reader, target, scb, collector)
        self._audit_stack_limits(target, collector, registers)
        self._audit_pacbti(target, collector, registers)
        if device_report.vendor == "STMicroelectronics" and device_report.product_line.is_available:
            self._audit_stm32_rdp(reader, device_report.product_line.display(), collector)

        return DiagnosticReport(
            self.service,
            target,
            (
                DiagnosticSection(
                    "Target",
                    (
                        DiagnosticField("vendor", device_report.vendor),
                        DiagnosticField(
                            "device_name",
                            (
                                device_report.product_line.display()
                                if device_report.product_line.is_available
                                else None
                            ),
                        ),
                    ),
                ),
            ),
            tuple(collector.findings),
        )

    def _audit_mpu(
        self,
        reader: TargetMemory,
        target: CortexMTargetDescription,
        collector: _FindingCollector,
        registers: CortexMRegisterReader,
    ) -> None:
        """Audit MPU configuration through the typed MPU inspection API."""
        category = "MPU"
        if not isinstance(reader, WritableTargetMemory):
            collector.add(
                category,
                DiagnosticSeverity.INFO,
                "MPU not accessible",
                "The target-memory backend does not support MPU region selection.",
            )
            return
        dump = dump_mpu_regions(reader, target)
        if not dump.is_available:
            collector.add(
                category,
                DiagnosticSeverity.INFO,
                "MPU not accessible",
                f"Cannot read MPU_TYPE: {dump.unavailable_reason}",
            )
            return
        assert dump.region_count is not None
        if dump.region_count == 0:
            collector.add(
                category,
                DiagnosticSeverity.INFO,
                "MPU not implemented",
                "MPU_TYPE.DREGION is 0.",
            )
            return

        control = dump.get_register("CTRL")
        if control is None or not control.is_available:
            collector.add(
                category,
                DiagnosticSeverity.INFO,
                "MPU control unavailable",
                (
                    control.unavailable_reason
                    if control is not None and control.unavailable_reason is not None
                    else "MPU_CTRL was not read."
                ),
            )
            return
        assert control.value is not None
        enabled = bool(control.value & 0x1)
        privdefena = bool(control.value & 0x4)
        if not enabled:
            collector.add(
                category,
                DiagnosticSeverity.ERROR,
                "MPU is disabled",
                f"MPU_CTRL.ENABLE=0 while {dump.region_count} region(s) are implemented; "
                "no memory protection is active.",
            )
        else:
            collector.add(
                category,
                DiagnosticSeverity.PASS,
                "MPU is enabled",
                f"{dump.region_count} region(s) implemented.",
            )

        regions = self._available_mpu_regions(dump, collector)
        enabled_regions = [region for region in regions if region.enabled]
        for region in enabled_regions:
            if region.end_address is None or region.end_address <= region.start_address:
                collector.add(
                    category,
                    DiagnosticSeverity.WARNING,
                    f"Region {region.index} has an invalid size",
                    f"base=0x{region.start_address:08X} " f"limit=0x{region.end_address or 0:08X}.",
                )
            if self._region_writable(region) and region.executable:
                collector.add(
                    category,
                    DiagnosticSeverity.ERROR,
                    f"Region {region.index} violates W^X",
                    f"0x{region.start_address:08X}-0x{region.end_address or 0:08X} "
                    "is both writable and executable.",
                )

        sorted_regions = sorted(enabled_regions, key=lambda region: region.start_address)
        for previous, current in zip(sorted_regions, sorted_regions[1:]):
            if self._overlaps(previous, current):
                collector.add(
                    category,
                    DiagnosticSeverity.ERROR,
                    f"Regions {previous.index} and {current.index} overlap",
                    f"0x{previous.start_address:08X}-0x{previous.end_address or 0:08X} "
                    f"overlaps 0x{current.start_address:08X}-0x{current.end_address or 0:08X}.",
                )

        if enabled:
            self._audit_stack_mpu_coverage(enabled_regions, privdefena, collector, registers)
        for region in enabled_regions:
            end_address = region.end_address
            if end_address is None:
                continue
            if (
                region.start_address < 0x40000000
                and end_address >= 0x20000000
                and region.executable
            ):
                collector.add(
                    category,
                    DiagnosticSeverity.ERROR,
                    f"Region {region.index} makes RAM executable",
                    f"0x{region.start_address:08X}-0x{end_address:08X} overlaps SRAM and is not XN.",
                )
            if region.start_address < 0x20000000 and end_address >= 0:
                if self._region_writable(region):
                    collector.add(
                        category,
                        DiagnosticSeverity.ERROR,
                        f"Region {region.index} makes Flash writable",
                        f"0x{region.start_address:08X}-0x{end_address:08X} "
                        "overlaps Flash/code and is writable.",
                    )
                if not region.executable:
                    collector.add(
                        category,
                        DiagnosticSeverity.WARNING,
                        f"Region {region.index} makes Flash non-executable",
                        f"0x{region.start_address:08X}-0x{end_address:08X} "
                        "overlaps Flash/code and sets XN.",
                    )

    def _available_mpu_regions(
        self,
        dump: MpuDump,
        collector: _FindingCollector,
    ) -> list[MpuRegion]:
        """Return readable regions and retain individual read failures as findings."""
        regions: list[MpuRegion] = []
        for result in dump.regions:
            if result.region is None:
                collector.add(
                    "MPU",
                    DiagnosticSeverity.INFO,
                    f"Region {result.index} unreadable",
                    result.unavailable_reason or "unknown MPU region read error",
                )
                continue
            regions.append(result.region)
        return regions

    def _audit_stack_mpu_coverage(
        self,
        regions: list[MpuRegion],
        privdefena: bool,
        collector: _FindingCollector,
        registers: CortexMRegisterReader,
    ) -> None:
        """Check whether current stack pointers have non-executable MPU coverage."""
        for name, register_names in (
            ("MSP", ("msp", "msp_ns", "msp_s")),
            ("PSP", ("psp", "psp_ns", "psp_s")),
        ):
            value = registers.read_first(register_names)
            if value is None:
                continue
            covering = next(
                (region for region in regions if self._region_covers_address(region, value)),
                None,
            )
            if covering is None:
                if privdefena:
                    collector.add(
                        "MPU",
                        DiagnosticSeverity.WARNING,
                        f"{name} falls back to the default background map",
                        f"0x{value:08X} is not covered by any explicit region; "
                        "MPU_CTRL.PRIVDEFENA=1 grants the default executable memory map.",
                    )
                else:
                    collector.add(
                        "MPU",
                        DiagnosticSeverity.ERROR,
                        f"{name} is not covered by any MPU region",
                        f"0x{value:08X} has no matching region and PRIVDEFENA=0 (access denied).",
                    )
            elif covering.executable:
                collector.add(
                    "MPU",
                    DiagnosticSeverity.ERROR,
                    f"{name} stack region is executable",
                    f"Region {covering.index} covering 0x{value:08X} does not set XN.",
                )
            else:
                collector.add(
                    "MPU",
                    DiagnosticSeverity.PASS,
                    f"{name} stack region is non-executable",
                    f"Region {covering.index} covers 0x{value:08X} with XN set.",
                )

    def _audit_cmsis_core(
        self,
        reader: TargetMemory,
        target: CortexMTargetDescription,
        scb: SystemRegisterSet,
        collector: _FindingCollector,
    ) -> None:
        """Audit typed SCB controls and the debug-halt control register."""
        category = "CMSIS Core Security"
        if target.core is not None and CortexMFeature.CONFIGURABLE_FAULTS in target.core.features:
            shcsr = self._scb_value(scb, "SHCSR")
            if shcsr is None:
                collector.add(
                    category,
                    DiagnosticSeverity.INFO,
                    "Configurable fault status unavailable",
                    "SHCSR could not be read.",
                )
            else:
                missing = [
                    name
                    for name, bit in (
                        ("MemManage", _SHCSR_MEMFAULTENA_BIT),
                        ("BusFault", _SHCSR_BUSFAULTENA_BIT),
                        ("UsageFault", _SHCSR_USGFAULTENA_BIT),
                    )
                    if not (shcsr & (1 << bit))
                ]
                if missing:
                    collector.add(
                        category,
                        DiagnosticSeverity.WARNING,
                        "Configurable fault handlers disabled",
                        f"{', '.join(missing)} disabled in SHCSR; these faults will escalate "
                        "to HardFault.",
                    )
                else:
                    collector.add(
                        category,
                        DiagnosticSeverity.PASS,
                        "Configurable fault handlers enabled",
                        "MemManage, BusFault, and UsageFault are all enabled in SHCSR.",
                    )
        else:
            collector.add(
                category,
                DiagnosticSeverity.INFO,
                "No configurable fault handlers on this core",
                f"{target.core_name} only implements HardFault.",
            )

        ccr = self._scb_value(scb, "CCR")
        if ccr is not None:
            if not (ccr & (1 << _CCR_DIV_0_TRP_BIT)):
                collector.add(
                    category,
                    DiagnosticSeverity.WARNING,
                    "Division-by-zero trap disabled",
                    "CCR.DIV_0_TRP=0: integer division by zero silently returns 0 instead of "
                    "faulting.",
                )
            if not (ccr & (1 << _CCR_UNALIGN_TRP_BIT)):
                collector.add(
                    category,
                    DiagnosticSeverity.INFO,
                    "Unaligned-access trap disabled",
                    "CCR.UNALIGN_TRP=0: unaligned accesses are silently allowed except for "
                    "LDM/STM/PUSH/POP.",
                )
            if ccr & (1 << _CCR_BFHFNMIGN_BIT):
                collector.add(
                    category,
                    DiagnosticSeverity.WARNING,
                    "BusFault ignored at high priority",
                    "CCR.BFHFNMIGN=1: precise data-bus faults are ignored in HardFault/NMI "
                    "handlers and priority -1/-2 code.",
                )

        try:
            dhcsr = reader.read_uint32(_DHCSR_ADDRESS)
        except TargetReadError as error:
            collector.add(category, DiagnosticSeverity.INFO, "DHCSR unavailable", str(error))
        else:
            if dhcsr & 0x1:
                collector.add(
                    category,
                    DiagnosticSeverity.INFO,
                    "Debug access is currently enabled",
                    "DHCSR.C_DEBUGEN=1: ensure the debug port is disabled/locked in production.",
                )
            else:
                collector.add(
                    category,
                    DiagnosticSeverity.PASS,
                    "Debug access is disabled",
                    "DHCSR.C_DEBUGEN=0.",
                )

    def _audit_trustzone(
        self,
        reader: TargetMemory,
        target: CortexMTargetDescription,
        collector: _FindingCollector,
    ) -> None:
        """Audit TrustZone configuration using the typed SAU inspection API."""
        category = "TrustZone (SAU)"
        if not isinstance(reader, WritableTargetMemory):
            collector.add(
                category,
                DiagnosticSeverity.INFO,
                "TrustZone-M not implemented",
                "The target-memory backend does not support SAU region selection.",
            )
            return
        status = read_sau_status(reader, target)
        if not status.is_available:
            collector.add(
                category,
                DiagnosticSeverity.INFO,
                "TrustZone-M not implemented",
                "SAU_TYPE is not accessible on this core.",
            )
            return
        dump = dump_sau_regions(reader, target, status)
        assert status.region_count is not None
        assert status.enabled is not None
        assert status.all_non_secure is not None
        if not status.enabled:
            if status.region_count == 0:
                collector.add(
                    category,
                    DiagnosticSeverity.INFO,
                    "TrustZone-M not used",
                    "SAU implements 0 regions and is disabled; this is likely a non-secure-only "
                    "build.",
                )
            else:
                collector.add(
                    category,
                    DiagnosticSeverity.WARNING,
                    "SAU implemented but disabled",
                    f"SAU_CTRL.ENABLE=0 with {status.region_count} region(s) available; "
                    "all memory is treated as Non-Secure and TrustZone is not enforced.",
                )
            return
        if status.all_non_secure:
            collector.add(
                category,
                DiagnosticSeverity.WARNING,
                "SAU_CTRL.ALLNS is set",
                "All memory defaults to Non-Secure outside matched regions; verify region coverage.",
            )

        regions = []
        for result in dump.regions:
            if result.region is None:
                collector.add(
                    category,
                    DiagnosticSeverity.INFO,
                    f"SAU region {result.index} unreadable",
                    result.unavailable_reason or "unknown SAU region read error",
                )
                continue
            regions.append(result.region)
        enabled_regions = [region for region in regions if region.enabled]
        if not enabled_regions:
            collector.add(
                category,
                DiagnosticSeverity.WARNING,
                "SAU is enabled without active regions",
                f"SAU_CTRL.ENABLE=1 but none of the {status.region_count} region(s) are enabled.",
            )
        else:
            collector.add(
                category,
                DiagnosticSeverity.PASS,
                "TrustZone-M active",
                f"{len(enabled_regions)} enabled Secure-attribution region(s) out of "
                f"{status.region_count}.",
            )
        sorted_regions = sorted(enabled_regions, key=lambda region: region.start_address)
        for previous, current in zip(sorted_regions, sorted_regions[1:]):
            if (
                previous.start_address <= current.end_address
                and current.start_address <= previous.end_address
            ):
                collector.add(
                    category,
                    DiagnosticSeverity.WARNING,
                    f"SAU regions {previous.index} and {current.index} overlap",
                    f"0x{previous.start_address:08X}-0x{previous.end_address:08X} overlaps "
                    f"0x{current.start_address:08X}-0x{current.end_address:08X}.",
                )

    def _audit_vtor(
        self,
        reader: TargetMemory,
        target: CortexMTargetDescription,
        scb: SystemRegisterSet,
        collector: _FindingCollector,
    ) -> None:
        """Audit the vector table and critical exception handler entries."""
        category = "Fault Handlers (VTOR)"
        vtor = self._scb_value(scb, "VTOR")
        if vtor is None:
            return
        collector.add(category, DiagnosticSeverity.INFO, "Vector table base", f"VTOR=0x{vtor:08X}.")
        shcsr = self._scb_value(scb, "SHCSR")
        configurable_faults = (
            target.core is not None and CortexMFeature.CONFIGURABLE_FAULTS in target.core.features
        )
        for index, name, enable_bit in _VECTOR_HANDLERS:
            if enable_bit is not None and not configurable_faults:
                continue
            try:
                handler = reader.read_uint32(vtor + 4 * index)
            except TargetReadError as error:
                collector.add(
                    category, DiagnosticSeverity.INFO, f"{name} handler unreadable", str(error)
                )
                continue
            if handler in (0x00000000, 0xFFFFFFFF):
                collector.add(
                    category,
                    DiagnosticSeverity.ERROR,
                    f"{name} handler is missing",
                    f"Vector[{index}]=0x{handler:08X}: the core will lock up on this exception.",
                )
                continue
            if not handler & 0x1:
                collector.add(
                    category,
                    DiagnosticSeverity.WARNING,
                    f"{name} handler is not Thumb-encoded",
                    f"Vector[{index}]=0x{handler:08X}: bit0 is clear, entry will UsageFault.",
                )
            else:
                collector.add(
                    category,
                    DiagnosticSeverity.PASS,
                    f"{name} handler is present",
                    f"0x{handler:08X}.",
                )
            if enable_bit is not None and shcsr is not None and not (shcsr & (1 << enable_bit)):
                collector.add(
                    category,
                    DiagnosticSeverity.WARNING,
                    f"{name} handler configured but not enabled",
                    f"SHCSR bit {enable_bit} is clear; faults will escalate to HardFault instead.",
                )

    def _audit_stack_limits(
        self,
        target: CortexMTargetDescription,
        collector: _FindingCollector,
        registers: CortexMRegisterReader,
    ) -> None:
        """Audit Armv8-M stack limits when the typed core description supports them."""
        if target.core is None or target.core.architecture_version not in _V81M_ARCHITECTURES | {
            CortexMArchitecture.ARMV8_M_BASELINE,
            CortexMArchitecture.ARMV8_M_MAINLINE,
        }:
            return
        category = "ARMv8-M Stack Limits"
        for name, stack_names, limit_names in (
            ("MSP", ("msp", "msp_ns", "msp_s"), ("msplim", "msplim_ns", "msplim_s")),
            ("PSP", ("psp", "psp_ns", "psp_s"), ("psplim", "psplim_ns", "psplim_s")),
        ):
            limit = registers.read_first(limit_names)
            if limit is None:
                collector.add(
                    category,
                    DiagnosticSeverity.INFO,
                    f"{name}LIM unavailable",
                    "GDB does not expose this register for the current target description.",
                )
                continue
            if limit == 0:
                collector.add(
                    category,
                    DiagnosticSeverity.WARNING,
                    f"{name}LIM is not configured",
                    f"{name}LIM=0x00000000: stack-overflow hardware detection is disabled for "
                    f"{name}.",
                )
            else:
                collector.add(
                    category,
                    DiagnosticSeverity.PASS,
                    f"{name}LIM is configured",
                    f"{name}LIM=0x{limit:08X}.",
                )
            stack_pointer = registers.read_first(stack_names)
            if stack_pointer is not None and limit != 0 and stack_pointer < limit:
                collector.add(
                    category,
                    DiagnosticSeverity.ERROR,
                    f"{name} is below its configured limit",
                    f"{name}=0x{stack_pointer:08X} < {name}LIM=0x{limit:08X}: "
                    "stack already overflowed.",
                )

    def _audit_pacbti(
        self,
        target: CortexMTargetDescription,
        collector: _FindingCollector,
        registers: CortexMRegisterReader,
    ) -> None:
        """Audit best-effort PAC key exposure for typed Armv8.1-M cores."""
        if target.core is None or target.core.architecture_version not in _V81M_ARCHITECTURES:
            return
        category = "PACBTI (ARMv8.1-M)"
        key_words = [registers.read_first((name,)) for name in _PAC_KEY_P_REGISTERS]
        if any(word is None for word in key_words):
            collector.add(
                category,
                DiagnosticSeverity.INFO,
                "PAC/BTI status cannot be determined",
                f"{target.core_name} may implement the optional Armv8.1-M PACBTI extension, but "
                "GDB does not expose the PAC_KEY_P_* registers for this target; the extension "
                "may not be implemented, or the security state/target description hides it.",
            )
            return
        if all(word == 0 for word in key_words):
            collector.add(
                category,
                DiagnosticSeverity.WARNING,
                "PAC privileged key is all-zero",
                "PAC_KEY_P_0..3 are all 0: return-address signing provides no real protection "
                "until firmware provisions a random, non-predictable key.",
            )
        else:
            collector.add(
                category,
                DiagnosticSeverity.PASS,
                "PAC privileged key material is present",
                "PAC_KEY_P_0..3 are populated; PAC is available for firmware compiled with "
                "return-address signing (-mbranch-protection=pac-ret).",
            )
        collector.add(
            category,
            DiagnosticSeverity.INFO,
            "BTI has no runtime enable bit",
            "Branch Target Identification is always active for code compiled with BTI landing "
            "pads on cores implementing the extension; verify the firmware was built with "
            "-mbranch-protection=bti (or pac-ret+bti) to benefit from it.",
        )

    def _audit_stm32_rdp(
        self,
        reader: TargetMemory,
        product_line: str,
        collector: _FindingCollector,
    ) -> None:
        """Decode STM32 Readout Protection using the documented Arm vendor layouts."""
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
                DiagnosticSeverity.INFO,
                "RDP layout not documented",
                f"No known Flash-option register layout for product line '{product_line}'.",
            )
            return
        address, shift = layout
        try:
            raw = reader.read_uint32(address)
        except TargetReadError as error:
            collector.add(category, DiagnosticSeverity.INFO, "RDP register unreadable", str(error))
            return
        rdp_byte = (raw >> shift) & 0xFF
        if rdp_byte == 0xAA:
            collector.add(
                category,
                DiagnosticSeverity.WARNING,
                "RDP level 0 (no protection)",
                f"0x{address:08X}=0x{raw:08X}.",
            )
        elif rdp_byte == 0xCC:
            collector.add(
                category,
                DiagnosticSeverity.PASS,
                "RDP level 2 (fully protected, debug disabled)",
                f"0x{address:08X}=0x{raw:08X}.",
            )
        else:
            collector.add(
                category,
                DiagnosticSeverity.PASS,
                "RDP level 1 (debug restricted)",
                f"0x{address:08X}=0x{raw:08X}, RDP byte=0x{rdp_byte:02X}.",
            )

    @staticmethod
    def _scb_value(scb: SystemRegisterSet, name: str) -> int | None:
        """Return an available SCB register value by architectural name."""
        register = scb.get(name)
        return register.value if register is not None else None

    @staticmethod
    def _region_writable(region: MpuRegion) -> bool:
        """Return whether an MPU access encoding permits writes."""
        return region.access in {
            MpuAccessPermission.PRIVILEGED_READ_WRITE,
            MpuAccessPermission.PRIVILEGED_READ_WRITE_USER_READ,
            MpuAccessPermission.READ_WRITE,
        }

    @staticmethod
    def _region_covers_address(region: MpuRegion, address: int) -> bool:
        """Return whether an MPU region enables access to an address."""
        if region.end_address is None or not region.start_address <= address <= region.end_address:
            return False
        if region.subregion_disable_mask is None or region.size_bytes is None:
            return True
        subregion_size = region.size_bytes // 8
        if subregion_size == 0:
            return True
        subregion = (address - region.start_address) // subregion_size
        return not (region.subregion_disable_mask & (1 << subregion))

    @staticmethod
    def _overlaps(left: MpuRegion, right: MpuRegion) -> bool:
        """Return whether two valid MPU regions cover a common address."""
        if left.end_address is None or right.end_address is None:
            return False
        return left.start_address <= right.end_address and right.start_address <= left.end_address
