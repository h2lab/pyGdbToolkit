# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Observe AArch64 privilege and exception controls without certifying isolation."""

from __future__ import annotations

from dataclasses import dataclass

from ..diagnostics import DiagnosticFinding, DiagnosticRegisterReader, DiagnosticSeverity
from .cpu import CpuRegister
from .features import FeatureReport, FeatureSupport
from .mmu import ExecutionContext, MmuReport, TranslationRegime


@dataclass(frozen=True)
class IsolationReport:
    """An isolation snapshot sharing existing MMU and execution-context evidence."""

    context: ExecutionContext
    mmu: MmuReport
    registers: tuple[CpuRegister, ...]

    def bits(self, name: str, shift: int, width: int = 1) -> int | None:
        """Reuse the register-evidence rules used by MMU configuration checks."""
        return MmuReport(None, self.registers).bits(name, shift, width)

    def level_support(self, level: int) -> FeatureSupport:
        """Decode EL2/EL3 presence without assuming that a lower EL implies a higher one."""
        if level not in (2, 3):
            raise ValueError("only optional EL2 and EL3 support can be queried")
        encoding = self.bits("ID_AA64PFR0_EL1", 4 * level, 4)
        if encoding == 0:
            return FeatureSupport.ABSENT
        if encoding in (1, 2):
            return FeatureSupport.PRESENT
        if encoding is None and self.context.exception_level == level:
            return FeatureSupport.PRESENT
        return FeatureSupport.UNKNOWN


def collect_isolation_report(
    reader: DiagnosticRegisterReader | None,
    context: ExecutionContext,
    features: FeatureReport,
    mmu: MmuReport,
) -> IsolationReport:
    """Read named evidence only, reusing previously attempted register reads even if missing."""
    registers = list(mmu.registers)
    if context.pstate is not None:
        registers.append(context.pstate)

    def read(name: str, aliases: tuple[str, ...] | None = None) -> None:
        if any(register.name == name for register in registers):
            return
        value = None if reader is None else reader.read_first(aliases or (name.lower(), name))
        registers.append(CpuRegister(name, 64, None if value is None else value & ((1 << 64) - 1)))

    read("ID_AA64PFR0_EL1")
    preliminary = IsolationReport(context, mmu, tuple(registers))
    if context.exception_level not in (0, 1, 2, 3):
        return preliminary
    for level, name in ((2, "HCR_EL2"), (3, "SCR_EL3")):
        if preliminary.level_support(level) is FeatureSupport.PRESENT:
            read(name)
    if context.exception_level in (1, 2, 3):
        read(f"VBAR_EL{context.exception_level}")
    if any(features.capability(name).support is FeatureSupport.PRESENT for name in ("PAN", "UAO")):
        read("PSTATE", ("pstate", "cpsr"))
    return IsolationReport(context, mmu, tuple(registers))


def audit_isolation(
    report: IsolationReport,
    features: FeatureReport,
) -> tuple[DiagnosticFinding, ...]:
    """Describe raw controls and contextual risks, never declaring global isolation PASS."""
    findings: list[DiagnosticFinding] = []

    def add(
        title: str, detail: str, severity: DiagnosticSeverity = DiagnosticSeverity.INFO
    ) -> None:
        findings.append(DiagnosticFinding("Isolation", severity, title, detail))

    def field(name: str, shift: int, width: int = 1) -> str:
        value = report.bits(name, shift, width)
        return "unknown" if value is None else str(value)

    add(
        "Isolation audit scope",
        "Named register observations only, for the selected CPU and instant. Effective security state, nested virtualization overrides, exception handlers, stage-2 mappings and system-wide isolation are not certified. No EL switch or instruction injection is performed.",
    )
    for level in (2, 3):
        support = report.level_support(level)
        encoding = report.bits("ID_AA64PFR0_EL1", level * 4, 4)
        source = (
            "current execution context"
            if encoding is None and support is FeatureSupport.PRESENT
            else f"ID_AA64PFR0_EL1.EL{level}={encoding if encoding is not None else 'unknown'}"
        )
        add(
            f"EL{level} availability",
            f"{support.value}; evidence: {source}. Presence does not establish enablement in the current Security state.",
        )
    current = report.context.exception_level
    mode = report.bits("PSTATE", 0, 5)
    valid_pstate = mode in (0, 4, 5, 8, 9, 12, 13) and current is not None and mode >> 2 == current
    host = report.mmu.regime is TranslationRegime.EL2_HOST and report.bits("HCR_EL2", 27) == 1
    applicable = current == 1 or (current == 2 and host)
    for name, shift in (("PAN", 22), ("UAO", 23)):
        support = features.capability(name).support
        value = report.bits("PSTATE", shift) if valid_pstate else None
        if support is not FeatureSupport.PRESENT:
            add(
                f"PSTATE.{name}",
                f"Not interpreted: {name} support is {support.value}; reserved PSTATE bits are not evidence of activation.",
            )
        elif value is None:
            add(
                f"PSTATE.{name}",
                "Unavailable or inconsistent AArch64 PSTATE evidence; no state is assumed.",
            )
        else:
            detail = f"PSTATE.{name}={value}; sampled GDB PSTATE bit {shift}. "
            if name == "PAN":
                detail += "PAN=1 restricts privileged data accesses to EL0-accessible addresses only in an applicable enabled stage-1 regime; nested virtualization can override it. PAN=0 may be transient during legitimate user access. "
            else:
                detail += "UAO=1 makes LDTR/STTR behave like privileged LDR/STR at EL1 or EL2 host with E2H=TGE=1. This is not an independent isolation enable bit. "
            detail += (
                "Applicable privilege context observed."
                if applicable
                else "Applicable EL1 or EL2 host context is not established; effect is not inferred."
            )
            mmu_enabled = (
                report.mmu.regime is not None
                and report.mmu.bits(f"SCTLR_{report.mmu.regime.suffix}", 0) == 1
            )
            add(
                f"PSTATE.{name}",
                detail,
                (
                    DiagnosticSeverity.WARNING
                    if name == "PAN" and value == 0 and applicable and mmu_enabled
                    else DiagnosticSeverity.INFO
                ),
            )
    span = None
    bank = "EL1" if current == 1 else "EL2" if current == 2 and host else None
    if bank is not None and features.capability("PAN").support is FeatureSupport.PRESENT:
        span = report.bits(f"SCTLR_{bank}", 23)
    if span is None:
        add(
            "PAN on exception entry",
            "SPAN is not interpreted: PAN support, applicable exception-entry context or SCTLR evidence is unavailable.",
        )
    else:
        detail = f"SCTLR_{bank}.SPAN={span}: " + (
            "PAN is set to 1 on the applicable exception entry."
            if span == 0
            else "PAN is preserved on exception entry, not forced to 1; software handling is not inspected."
        )
        if features.capability("PAN").encoding == 3:
            detail += f" EPAN={field(f'SCTLR_{bank}', 57)} (PAN3); its interaction with execution permissions is not audited."
        add(
            "PAN on exception entry",
            detail,
            DiagnosticSeverity.WARNING if span else DiagnosticSeverity.INFO,
        )
    hcr = report.bits("HCR_EL2", 0, 64)
    if report.level_support(2) is not FeatureSupport.PRESENT or current is None:
        add(
            "EL2 configuration",
            "Not interpreted: EL2 presence or current context is not established.",
        )
    else:
        detail = (
            "HCR_EL2="
            + ("unavailable" if hcr is None else f"0x{hcr:016X}")
            + "; observed controls: "
        )
        detail += ", ".join(
            f"{name}={field('HCR_EL2', shift)}"
            for name, shift in (
                ("VM", 0),
                ("FMO", 3),
                ("IMO", 4),
                ("AMO", 5),
                ("DC", 12),
                ("TVM", 26),
                ("TGE", 27),
                ("TRVM", 30),
                ("RW", 31),
            )
        )
        e2h = (
            field("HCR_EL2", 34)
            if features.capability("VHE").support is FeatureSupport.PRESENT
            else "not interpreted (VHE absent/unknown)"
        )
        add(
            "EL2 configuration",
            detail
            + f", E2H={e2h}. VM is a requested guest stage-2 control, not evidence of guest isolation. E2H/TGE, security-state enablement and other overrides determine effective routing/traps; no zero bit is treated as a policy failure.",
        )
    scr = report.bits("SCR_EL3", 0, 64)
    if report.level_support(3) is not FeatureSupport.PRESENT or current is None:
        add(
            "EL3 configuration",
            "Not interpreted: EL3 presence or current context is not established.",
        )
    else:
        detail = (
            "SCR_EL3="
            + ("unavailable" if scr is None else f"0x{scr:016X}")
            + "; observed controls: "
        )
        detail += ", ".join(
            f"{name}={field('SCR_EL3', shift)}"
            for name, shift in (
                ("NS", 0),
                ("IRQ", 1),
                ("FIQ", 2),
                ("EA", 3),
                ("SMD", 7),
                ("RW", 10),
            )
        )
        if report.level_support(2) is FeatureSupport.PRESENT:
            detail += f", HCE={field('SCR_EL3', 8)}"
        sel2 = report.bits("ID_AA64PFR0_EL1", 36, 4)
        rme = report.bits("ID_AA64PFR0_EL1", 52, 4)
        if sel2 == 1:
            detail += f", EEL2={field('SCR_EL3', 18)}"
        if rme in (1, 2):
            detail += f", NSE={field('SCR_EL3', 62)}"
        add(
            "EL3 configuration",
            detail
            + ". NS/RW configure lower exception levels, not the execution/security state of EL3. NS alone is not used to classify Secure/Non-secure/Realm; routing and call controls are policy-dependent, not global security verdicts.",
        )
    vbar_name = f"VBAR_EL{current}"
    vbar = report.bits(vbar_name, 0, 64) if current in (1, 2, 3) else None
    if vbar is None:
        add(
            "Exception vector base",
            "Current-level VBAR is unavailable or no privileged EL is identified. No handler memory is read.",
        )
    else:
        add(
            "Exception vector base",
            f"{vbar_name}=0x{vbar:016X}; "
            + (
                "bits[10:0] violate the RES0 alignment requirement."
                if vbar & 0x7FF
                else "2 KiB aligned (zero is not automatically invalid)."
            )
            + " Canonical address constraints, mappings and handler contents are not inspected.",
            DiagnosticSeverity.WARNING if vbar & 0x7FF else DiagnosticSeverity.INFO,
        )
    return tuple(findings)
