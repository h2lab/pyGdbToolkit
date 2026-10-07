# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Observe PAC, BTI and MTE controls without reading authentication keys or tags."""

from __future__ import annotations

from dataclasses import replace

from ..diagnostics import DiagnosticFinding, DiagnosticRegisterReader, DiagnosticSeverity
from .cpu import CpuRegister
from .features import FeatureReport, FeatureSupport
from .isolation import IsolationReport
from .mmu import TranslationRegime

_ADDRESS_PAC = (
    "PAC address QARMA5",
    "PAC address implementation-defined",
    "PAC address QARMA3",
)
_GENERIC_PAC = (
    "PAC generic QARMA5",
    "PAC generic implementation-defined",
    "PAC generic QARMA3",
)


def pac_support(features: FeatureReport, names: tuple[str, ...]) -> FeatureSupport:
    """Combine alternative algorithms without treating missing algorithms as absent."""
    states = tuple(features.capability(name).support for name in names)
    if states.count(FeatureSupport.PRESENT) > 1:
        return FeatureSupport.UNKNOWN
    if FeatureSupport.PRESENT in states:
        return FeatureSupport.PRESENT
    if all(state is FeatureSupport.ABSENT for state in states):
        return FeatureSupport.ABSENT
    return FeatureSupport.UNKNOWN


def collect_protection_report(
    reader: DiagnosticRegisterReader | None,
    isolation: IsolationReport,
    features: FeatureReport,
) -> IsolationReport:
    """Reuse sampled controls and request only PSTATE when tag checking is implemented."""
    mte = features.capability("MTE")
    if (
        mte.support is not FeatureSupport.PRESENT
        or mte.encoding not in (2, 3)
        or isolation.mmu.regime is None
        or any(register.name == "PSTATE" for register in isolation.registers)
    ):
        return isolation
    value = None if reader is None else reader.read_first(("pstate", "cpsr"))
    pstate = CpuRegister("PSTATE", 64, None if value is None else value & ((1 << 64) - 1))
    return replace(isolation, registers=(*isolation.registers, pstate))


def audit_protection(
    report: IsolationReport,
    features: FeatureReport,
) -> tuple[DiagnosticFinding, ...]:
    """Audit sampled configuration, not binary instrumentation or protected mappings."""
    findings: list[DiagnosticFinding] = [
        DiagnosticFinding(
            "Protection",
            DiagnosticSeverity.INFO,
            "PAC/BTI/MTE audit scope",
            "Conditional configuration observations only. No authentication key registers, "
            "allocation tags, instructions or translation descriptors are read. "
            "Binary instrumentation, key quality, guarded pages and effective protection are not certified.",
        )
    ]
    findings.extend(_audit_pac(report, features))
    findings.extend(_audit_bti(report, features))
    findings.extend(_audit_mte(report, features))
    return tuple(findings)


def _finding(
    category: str,
    title: str,
    detail: str,
    severity: DiagnosticSeverity = DiagnosticSeverity.INFO,
) -> DiagnosticFinding:
    return DiagnosticFinding(category, severity, title, detail)


def _controls(
    report: IsolationReport,
    register: str,
    fields: tuple[tuple[str, int, int], ...],
) -> str:
    """Render only fields whose complete bit evidence is available."""
    return f"{register}: " + ", ".join(
        f"{name}={value if (value := report.bits(register, shift, width)) is not None else 'unknown'}"
        for name, shift, width in fields
    )


def _audit_pac(report: IsolationReport, features: FeatureReport) -> list[DiagnosticFinding]:
    address = pac_support(features, _ADDRESS_PAC)
    generic = pac_support(features, _GENERIC_PAC)
    detail = (
        f"Address authentication support: {address.value}; generic PACGA support: {generic.value}. "
    )
    regime = report.mmu.regime
    if address is not FeatureSupport.PRESENT or regime is None:
        detail += "Address enable bits are not interpreted: address PAC support or translation regime is not established. Generic PACGA support alone does not establish address signing."
        return [_finding("PAC", "Address authentication controls", detail)]
    register = f"SCTLR_{regime.suffix}"
    fields = (("EnIA", 31, 1), ("EnIB", 30, 1), ("EnDA", 27, 1), ("EnDB", 13, 1))
    detail += _controls(report, register, fields)
    detail += ". These bits request address signing/authentication behavior; generic PACGA is not controlled by these bits. Code use, keys and trap emulation are not assessed."
    values = tuple(report.bits(register, shift) for _, shift, _ in fields)
    findings = [
        _finding(
            "PAC",
            "Address authentication controls",
            detail,
            DiagnosticSeverity.WARNING if values == (0, 0, 0, 0) else DiagnosticSeverity.INFO,
        )
    ]
    traps: list[str] = []
    if report.context.exception_level == 1 and report.level_support(2) is FeatureSupport.PRESENT:
        traps.append(_controls(report, "HCR_EL2", (("API", 41, 1), ("APK", 40, 1))))
    if (
        report.context.exception_level in (1, 2)
        and report.level_support(3) is FeatureSupport.PRESENT
    ):
        traps.append(_controls(report, "SCR_EL3", (("API", 17, 1), ("APK", 16, 1))))
    if traps:
        findings.append(
            _finding(
                "PAC",
                "Authentication trap controls",
                "; ".join(traps)
                + ". API/APK=0 can trap enabled instructions/key-register accesses; 1 does not trap by this control. Security-state and host overrides remain unqualified. Keys are never read.",
            )
        )
    return findings


def _el0_controls(report: IsolationReport) -> bool:
    """Qualify the local bank's EL0 fields for EL1 or a positively selected VHE host."""
    return report.mmu.regime is TranslationRegime.EL1 or (
        report.mmu.regime is TranslationRegime.EL2_HOST and report.bits("HCR_EL2", 27) == 1
    )


def _audit_bti(report: IsolationReport, features: FeatureReport) -> list[DiagnosticFinding]:
    support = features.capability("BTI").support
    regime = report.mmu.regime
    if support is not FeatureSupport.PRESENT or regime is None:
        return [
            _finding(
                "BTI",
                "Branch target compatibility",
                f"Not interpreted: BTI support is {support.value} or the translation regime is unavailable.",
            )
        ]
    name = "BT1" if regime is TranslationRegime.EL1 or _el0_controls(report) else "BT"
    fields: tuple[tuple[str, int, int], ...] = ((name, 36, 1),)
    if _el0_controls(report):
        fields += (("BT0", 35, 1),)
    detail = _controls(report, f"SCTLR_{regime.suffix}", fields)
    detail += ". BT*=0 permits PACIASP/PACIBSP compatibility with BTYPE=3; BT*=1 excludes that compatibility. These are not global BTI enable bits. Guarded-page attributes, landing pads and branch coverage are not inspected. EL0 field effects at EL1 remain subject to higher-level host overrides."
    return [_finding("BTI", "Branch target compatibility", detail)]


def _tcf_description(value: int | None, level: int, asynchronous: int | None) -> str:
    """Preserve MTE3 and MTE fractional-field requirements for fault modes."""
    if value is None:
        return "unknown"
    if value == 3 and level != 3:
        return "reserved without MTE3"
    if value == 2 and level == 2 and asynchronous != 0:
        return "asynchronous mode support unknown/absent (MTE_frac)"
    return {0: "fault reporting disabled", 1: "synchronous", 2: "asynchronous", 3: "asymmetric"}[
        value
    ]


def _audit_mte(report: IsolationReport, features: FeatureReport) -> list[DiagnosticFinding]:
    mte = features.capability("MTE")
    regime = report.mmu.regime
    if mte.support is not FeatureSupport.PRESENT or regime is None:
        return [
            _finding(
                "MTE",
                "Tag checking configuration",
                f"Not interpreted: MTE support is {mte.support.value} or the translation regime is unavailable.",
            )
        ]
    if mte.encoding == 1:
        return [
            _finding(
                "MTE",
                "Tag checking configuration",
                "Instruction-only MTE: allocation tags and tag checking are not implemented by this level. ATA/TCF/TCO are not interpreted.",
            )
        ]
    assert mte.encoding in (2, 3)
    register = f"SCTLR_{regime.suffix}"
    value = report.bits(register, 40, 2)
    id_report = IsolationReport(report.context, report.mmu, features.registers)
    asynchronous = id_report.bits("ID_AA64PFR1_EL1", 40, 4)
    detail = _controls(report, register, (("ATA", 43, 1), ("TCF", 40, 2)))
    detail += f"; TCF mode: {_tcf_description(value, mte.encoding, asynchronous)}. "
    severity = (
        DiagnosticSeverity.WARNING if value == 3 and mte.encoding == 2 else DiagnosticSeverity.INFO
    )
    findings = [
        _finding(
            "MTE",
            "Tag checking configuration",
            detail
            + "ATA=0 prevents allocation-tag access and tag checks by this control; TCF=0 disables tag-fault reporting. Neither ATA=1 nor TCF!=0 certifies tagged mappings or complete enforcement.",
            severity,
        )
    ]
    if _el0_controls(report):
        value0 = report.bits(register, 38, 2)
        findings.append(
            _finding(
                "MTE",
                "EL0 tag checking controls",
                _controls(report, register, (("ATA0", 42, 1), ("TCF0", 38, 2)))
                + f"; TCF0 mode: {_tcf_description(value0, mte.encoding, asynchronous)}. EL0 effective bank selection and higher-level overrides are not certified.",
                (
                    DiagnosticSeverity.WARNING
                    if value0 == 3 and mte.encoding == 2
                    else DiagnosticSeverity.INFO
                ),
            )
        )
    mode = report.bits("PSTATE", 0, 5)
    valid = mode in (0, 4, 5, 8, 9, 12, 13) and mode >> 2 == report.context.exception_level
    tco = report.bits("PSTATE", 25) if valid else None
    findings.append(
        _finding(
            "MTE",
            "Tag check override",
            f"PSTATE.TCO={'unknown' if tco is None else tco}. TCO=1 suppresses tag checking for affected accesses and may be transient; unavailable/inconsistent PSTATE is never interpreted as TCO=0.",
        )
    )
    tcr = f"TCR_{regime.suffix}"
    fields = (
        (("TBI0", 37, 1), ("TBI1", 38, 1), ("TCMA0", 57, 1), ("TCMA1", 58, 1))
        if regime.dual_range
        else (("TBI", 20, 1), ("TCMA", 30, 1))
    )
    findings.append(
        _finding(
            "MTE",
            "Tag address controls",
            _controls(report, tcr, fields)
            + ". TBI permits address tags but is not MTE enablement. TCMA can mark matching logical tags as unchecked. Extended tag modes, memory attributes, allocation tags and fault status are not inspected.",
        )
    )
    gates: list[str] = []
    if report.context.exception_level == 1 and report.level_support(2) is FeatureSupport.PRESENT:
        gates.append(_controls(report, "HCR_EL2", (("ATA", 56, 1),)))
    if (
        report.context.exception_level in (1, 2)
        and report.level_support(3) is FeatureSupport.PRESENT
    ):
        gates.append(_controls(report, "SCR_EL3", (("ATA", 26, 1),)))
    if gates:
        findings.append(
            _finding(
                "MTE",
                "Higher-level tag access controls",
                "; ".join(gates)
                + ". Zero can prevent lower-level tag access/checking. Effective security-state/host overrides remain unqualified; one is not proof of MTE enforcement.",
            )
        )
    return findings
