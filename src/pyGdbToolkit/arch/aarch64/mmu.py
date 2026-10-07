# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Inspect named AArch64 stage-1 MMU controls without walking translation tables."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ..diagnostics import DiagnosticFinding, DiagnosticRegisterReader, DiagnosticSeverity
from .cpu import CpuRegister, physical_address_bits
from .features import FeatureReport, FeatureSupport


class _FindingWriter(Protocol):
    """Append portable findings while preserving informational defaults."""

    def __call__(
        self,
        title: str,
        detail: str,
        severity: DiagnosticSeverity = DiagnosticSeverity.INFO,
    ) -> None:
        """Append an MMU finding with an optional informational severity."""
        ...


@dataclass(frozen=True)
class ExecutionContext:
    """A sampled exception level and the evidence used to identify it."""

    exception_level: int | None
    detail: str


def collect_execution_context(reader: DiagnosticRegisterReader | None) -> ExecutionContext:
    """Read CurrentEL or a valid PSTATE mode once for all audit passes."""
    if reader is None:
        return ExecutionContext(
            None, "No runtime register reader is available; the current EL is unknown."
        )
    current_el = reader.read_first(("currentel", "CurrentEL"))
    if current_el is not None:
        if current_el in (0, 4, 8, 12):
            return ExecutionContext(
                current_el >> 2, f"EL{current_el >> 2}, observed through CurrentEL."
            )
        return ExecutionContext(
            None, "CurrentEL has an invalid encoding; the current EL is unknown."
        )
    pstate = reader.read_first(("pstate", "cpsr"))
    if pstate is None:
        return ExecutionContext(
            None, "CurrentEL and PSTATE are not exposed or readable; the current EL is unknown."
        )
    mode = pstate & 0x1F
    if mode not in (0, 4, 5, 8, 9, 12, 13):
        return ExecutionContext(
            None, "PSTATE does not identify a valid AArch64 mode; the current EL is unknown."
        )
    return ExecutionContext(mode >> 2, f"EL{mode >> 2}, observed through GDB PSTATE.M[3:2].")


class TranslationRegime(StrEnum):
    """Register layouts supported by this configuration-only audit."""

    EL1 = "EL1&0"
    EL2 = "EL2"
    EL2_HOST = "EL2&0 (VHE)"
    EL3 = "EL3"

    @property
    def suffix(self) -> str:
        """Return the architectural register bank rather than an accessor alias."""
        return {self.EL1: "EL1", self.EL2: "EL2", self.EL2_HOST: "EL2", self.EL3: "EL3"}[self]

    @property
    def dual_range(self) -> bool:
        """Whether this layout has TTBR1 and the EL1-style TCR fields."""
        return self in (self.EL1, self.EL2_HOST)


@dataclass(frozen=True)
class MmuReport:
    """Raw configuration evidence and a positively selected register layout."""

    regime: TranslationRegime | None
    registers: tuple[CpuRegister, ...]
    unavailable_reason: str | None = None

    def bits(self, name: str, shift: int, width: int = 1) -> int | None:
        """Decode a field only when every required bit was observed."""
        register = next((register for register in self.registers if register.name == name), None)
        if register is None or register.value is None:
            return None
        if min(register.width_bits, register.valid_bits) < shift + width:
            return None
        return (register.value >> shift) & ((1 << width) - 1)


def collect_mmu_report(
    reader: DiagnosticRegisterReader | None,
    context: ExecutionContext,
    features: FeatureReport,
) -> MmuReport:
    """Read only the identified regime, never switching EL or executing instructions."""
    registers: list[CpuRegister] = []

    def read(name: str) -> CpuRegister:
        value = None if reader is None else reader.read_first((name.lower(), name))
        register = CpuRegister(name, 64, None if value is None else value & ((1 << 64) - 1))
        registers.append(register)
        return register

    match context.exception_level:
        case 1:
            regime = TranslationRegime.EL1
        case 3:
            regime = TranslationRegime.EL3
        case 2:
            vhe = features.capability("VHE").support
            if vhe is FeatureSupport.ABSENT:
                regime = TranslationRegime.EL2
            else:
                hcr = read("HCR_EL2").value
                if hcr is None:
                    return MmuReport(
                        None,
                        tuple(registers),
                        "EL2 layout is unknown: HCR_EL2.E2H is unavailable and VHE is not known absent.",
                    )
                if hcr & (1 << 34):
                    if vhe is not FeatureSupport.PRESENT:
                        return MmuReport(
                            None,
                            tuple(registers),
                            "HCR_EL2.E2H=1 but VHE support is unknown; host layout is not assumed.",
                        )
                    regime = TranslationRegime.EL2_HOST
                else:
                    regime = TranslationRegime.EL2
        case 0:
            return MmuReport(
                None, (), "EL0 host/guest translation regime is not resolved; EL1 is not assumed."
            )
        case _:
            return MmuReport(None, (), "Current EL is unknown; no MMU register bank is assumed.")
    for prefix in ("SCTLR", "TCR", "TTBR0", "MAIR"):
        read(f"{prefix}_{regime.suffix}")
    if regime.dual_range:
        read(f"TTBR1_{regime.suffix}")
    return MmuReport(regime, tuple(registers))


def audit_mmu(report: MmuReport, features: FeatureReport) -> tuple[DiagnosticFinding, ...]:
    """Audit observed configuration, leaving effective mappings and stage 2 unqualified."""
    findings: list[DiagnosticFinding] = []

    def add(
        title: str, detail: str, severity: DiagnosticSeverity = DiagnosticSeverity.INFO
    ) -> None:
        findings.append(DiagnosticFinding("MMU", severity, title, detail))

    add(
        "MMU audit scope",
        "Stage-1 register configuration only. No translation tables or TLB entries are read. Stage 2, EL/security-state overrides and per-mapping W^X are not audited.",
    )
    if report.regime is None:
        add("MMU regime unavailable", report.unavailable_reason or "Translation regime is unknown.")
        return tuple(findings)
    regime = report.regime
    suffix = regime.suffix
    add(
        "Translation regime",
        f"{regime.value} register layout selected from the observed context. Configuration is not proof of effective memory protection.",
    )
    for register in report.registers:
        full = report.bits(register.name, 0, 64)
        add(
            register.name,
            (
                f"0x{full:016X}; source: {register.source}."
                if full is not None
                else "Full register value is not exposed or readable; no zero value is assumed."
            ),
        )
    mair = f"MAIR_{suffix}"
    attributes = [report.bits(mair, index * 8, 8) for index in range(8)]
    add(
        "Memory attribute slots",
        "; ".join(
            f"Attr{index}={'unknown' if value is None else f'0x{value:02X}'}"
            for index, value in enumerate(attributes)
        )
        + ". Slot use and feature-dependent attribute encodings require descriptor inspection; no memory-type policy is inferred.",
    )
    sctlr_name = f"SCTLR_{suffix}"
    enabled = report.bits(sctlr_name, 0)
    if enabled is None:
        add("Stage-1 MMU control", f"{sctlr_name}.M is unavailable.")
    else:
        add(
            "Stage-1 MMU control",
            f"{sctlr_name}.M={enabled}: stage-1 translation is requested {'enabled' if enabled else 'disabled'} by this register. Overrides and other translation stages are not assessed.",
            DiagnosticSeverity.INFO if enabled else DiagnosticSeverity.WARNING,
        )
    wxn = report.bits(sctlr_name, 19)
    if wxn is None or enabled != 1:
        add(
            "Write-implies-execute-never control",
            "WXN enforcement is not assessed because SCTLR.M is not observed enabled or WXN is unreadable.",
        )
    else:
        add(
            "Write-implies-execute-never control",
            f"{sctlr_name}.WXN={wxn}. This bit alone does not establish per-mapping W^X; descriptor formats, permission schemes and overrides are not checked.",
            DiagnosticSeverity.INFO if wxn else DiagnosticSeverity.WARNING,
        )
    if enabled != 1:
        add(
            "MMU consistency checks skipped",
            "SCTLR.M is not observed enabled. Raw configuration is retained, but inactive or unavailable controls do not receive consistency verdicts.",
        )
        return tuple(findings)
    _audit_tcr(report, features, add)
    return tuple(findings)


def _feature_bits(features: FeatureReport, name: str, shift: int, width: int) -> int | None:
    """Reuse the feature snapshot without rereading ID registers."""
    return MmuReport(None, features.registers).bits(name, shift, width)


def _audit_tcr(
    report: MmuReport,
    features: FeatureReport,
    add: _FindingWriter,
) -> None:
    """Decode common geometry and only feature-qualified optional controls."""
    assert report.regime is not None
    regime = report.regime
    tcr = f"TCR_{regime.suffix}"
    ds = report.bits(tcr, 59 if regime.dual_range else 32)
    if ds is None or ds == 1:
        add(
            "Extended translation format",
            f"{tcr}.DS={'unknown' if ds is None else 1}. "
            "Geometry consistency checks are skipped because the descriptor format "
            "is unavailable or extended; LPA2 and D128 are not qualified.",
        )
        return
    ps_shift = 32 if regime.dual_range else 16
    ps = report.bits(tcr, ps_shift, 3)
    parange = _feature_bits(features, "ID_AA64MMFR0_EL1", 0, 4)
    maximum = None if parange is None else physical_address_bits(parange)
    if ps is None:
        add("Output address size", f"{tcr} address-size field is unavailable.")
    elif ps >= 6:
        add(
            "Output address size",
            f"{tcr} address-size encoding=0x{ps:X}. Extended 52/56-bit formats and granule-dependent effective sizes are not qualified by this audit.",
        )
    else:
        configured = physical_address_bits(ps)
        detail = f"{tcr} requests {configured}-bit output addresses; CPU PARange={'unknown' if maximum is None else str(maximum) + ' bits'}."
        add(
            "Output address size",
            detail,
            (
                DiagnosticSeverity.ERROR
                if maximum is not None and configured is not None and configured > maximum
                else DiagnosticSeverity.INFO
            ),
        )
    for index in range(2 if regime.dual_range else 1):
        shift = 16 * index
        size = report.bits(tcr, shift, 6)
        granule_encoding = report.bits(tcr, 30 if index else 14, 2)
        granule = (
            ({1: 16, 2: 4, 3: 64} if index else {0: 4, 1: 64, 2: 16}).get(granule_encoding)
            if granule_encoding is not None
            else None
        )
        epd = report.bits(tcr, 23 if index else 7) if regime.dual_range else 0
        if epd is None:
            add(
                f"Translation range {index}",
                f"{tcr}.EPD{index} is unavailable; geometry checks are skipped.",
            )
            continue
        if epd == 1:
            add(
                f"Translation range {index}",
                f"{tcr}.EPD{index}=1: table walks are disabled on TLB misses, not necessarily cached translations. Geometry checks are skipped.",
            )
            continue
        detail = f"T{index}SZ={'unknown' if size is None else size}; programmed VA bits={'unknown' if size is None else 64 - size}; TG{index}={'unknown/reserved' if granule is None else str(granule) + ' KiB'}. Exact TxSZ bounds and extended descriptor formats are not qualified."
        add(
            f"Translation range {index}",
            detail,
            (
                DiagnosticSeverity.WARNING
                if granule_encoding is not None and granule is None
                else DiagnosticSeverity.INFO
            ),
        )
        if granule is not None:
            id_shift = {4: 28, 16: 20, 64: 24}[granule]
            id_value = _feature_bits(features, "ID_AA64MMFR0_EL1", id_shift, 4)
            absent = 0 if granule == 16 else 15
            present = (1, 2) if granule == 16 else ((0, 1) if granule == 4 else (0,))
            support = (
                "absent" if id_value == absent else "present" if id_value in present else "unknown"
            )
            add(
                f"Granule support {index}",
                f"ID_AA64MMFR0_EL1 reports {granule} KiB granule support {support}. Unsupported programmed sizes can fall back to an implementation-defined granule.",
                DiagnosticSeverity.WARNING if support == "absent" else DiagnosticSeverity.INFO,
            )
        shareability = report.bits(tcr, 28 if index else 12, 2)
        if shareability == 1:
            add(
                f"Table-walk shareability {index}",
                f"{tcr}.SH{index}=0b01 is a reserved encoding.",
                DiagnosticSeverity.ERROR,
            )
    ha_shift, hd_shift = (39, 40) if regime.dual_range else (21, 22)
    hafdbs = features.capability("HAFDBS")
    ha = report.bits(tcr, ha_shift)
    hd = report.bits(tcr, hd_shift)
    if hafdbs.support is FeatureSupport.PRESENT and ha is not None and hd is not None:
        detail = f"{tcr}.HA={ha}, HD={hd}; ID HAFDBS=0x{hafdbs.encoding:X}. HD is effective only with HA=1."
        inconsistent = hd == 1 and (ha == 0 or hafdbs.encoding == 1)
        add(
            "Hardware access/dirty updates",
            detail,
            DiagnosticSeverity.WARNING if inconsistent else DiagnosticSeverity.INFO,
        )
    else:
        add(
            "Hardware access/dirty updates",
            "HA/HD are not interpreted: feature support or control bits are unavailable, or HAFDBS is absent.",
        )
    hpds = features.capability("HPDS")
    for index, hpd_shift in enumerate((41, 42) if regime.dual_range else (24,)):
        hpd = report.bits(tcr, hpd_shift)
        if hpds.support is FeatureSupport.PRESENT and hpd is not None:
            add(
                f"Hierarchical permissions {index}",
                f"{tcr}.HPD{'%d' % index if regime.dual_range else ''}={hpd}: hierarchical permissions {'disabled' if hpd else 'enabled'}. Leaf permissions are not inspected.",
                DiagnosticSeverity.WARNING if hpd else DiagnosticSeverity.INFO,
            )
        else:
            add(
                f"Hierarchical permissions {index}",
                "HPD is not interpreted: HPDS support is absent/unknown or the control bit is unavailable.",
            )
