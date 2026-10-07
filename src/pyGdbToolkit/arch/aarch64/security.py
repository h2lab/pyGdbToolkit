# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""AArch64 capability, MMU and isolation audit behind the portable diagnostic API."""

from __future__ import annotations

from ...target_memory import TargetMemory
from ..base import Architecture, TargetDescription
from ..diagnostics import (
    DiagnosticFinding,
    DiagnosticRegisterReader,
    DiagnosticReport,
    DiagnosticRuntimeAccess,
    DiagnosticServiceName,
    DiagnosticSeverity,
)
from .cpu import decode_midr
from .features import collect_feature_report
from .isolation import audit_isolation, collect_isolation_report
from .mmu import audit_mmu, collect_execution_context, collect_mmu_report
from .target import AArch64TargetDescription


class AArch64SecurityAuditor:
    """Audit observed controls without certifying uninspected memory mappings."""

    architecture = Architecture.AARCH64
    service = DiagnosticServiceName.SECURITY_AUDIT

    def __init__(self, registers: DiagnosticRegisterReader | None = None) -> None:
        """Accept an injected register source or the portable runtime access."""
        self._registers = registers

    def supports(self, target: TargetDescription) -> bool:
        """Accept AArch64 descriptions independently of the CPU model."""
        return isinstance(target, AArch64TargetDescription)

    def collect(
        self,
        reader: TargetMemory,
        target: TargetDescription,
        access: DiagnosticRuntimeAccess | None = None,
    ) -> DiagnosticReport:
        """Collect fresh named registers only, preserving unknown target state."""
        if not isinstance(target, AArch64TargetDescription):
            raise ValueError("AArch64 security auditing requires an AArch64 target description")
        del reader
        registers = (
            access.registers
            if access is not None and access.registers is not None
            else self._registers
        )
        features = collect_feature_report(registers)
        midr = None if registers is None else registers.read_first(("midr_el1", "MIDR_EL1"))
        context = collect_execution_context(registers)
        mmu = collect_mmu_report(registers, context, features)
        isolation = collect_isolation_report(registers, context, features, mmu)
        if midr is None:
            identity_detail = "MIDR_EL1 is not exposed or readable; CPU identity is unknown."
        else:
            identity = decode_midr(midr & 0xFFFFFFFF)
            identity_detail = (
                f"{identity.core_name} {identity.rnp_revision}; "
                f"MIDR_EL1[31:0]=0x{midr & 0xFFFFFFFF:08X}. "
                "The CPU revision is not an ARMv8-A architecture minor version."
            )
        findings: tuple[DiagnosticFinding, ...] = (
            DiagnosticFinding(
                "Scope",
                DiagnosticSeverity.INFO,
                "AArch64 audit coverage is limited",
                "CPU identity, execution context, selected ID capabilities, stage-1 MMU and isolation controls are observed. "
                "Mapping permissions, effective system-wide isolation and optional security extension activation are not audited. "
                "This report does not establish a secure configuration.",
            ),
            DiagnosticFinding(
                "CPU context", DiagnosticSeverity.INFO, "CPU identity", identity_detail
            ),
            DiagnosticFinding(
                "CPU context",
                DiagnosticSeverity.INFO,
                "Current exception level",
                context.detail,
            ),
            DiagnosticFinding(
                "Architecture",
                DiagnosticSeverity.INFO,
                "ARMv8-A minor version is undetermined",
                "GDB AArch64 metadata and MIDR_EL1 do not establish an ARMv8.x-A version. "
                "Observed feature-family introduction generations: "
                f"{', '.join(features.observed_generations) or 'none established'}. "
                "These are feature history, not a minimum or exact CPU architecture version. "
                "This inventory is not a complete architecture conformance check; "
                "ID values can also describe a virtualized CPU.",
            ),
        )
        findings += tuple(
            DiagnosticFinding(
                "Capabilities",
                DiagnosticSeverity.INFO,
                capability.field.name,
                f"{capability.support.value}: {capability.description} "
                f"{capability.field.register}.{capability.field.field}="
                f"{f'0x{capability.encoding:X}' if capability.encoding is not None else 'unknown'}; "
                f"source: {capability.source}. "
                f"Feature family introduced in {capability.field.introduced_in}. "
                "Presence does not establish runtime enablement or protection.",
            )
            for capability in features.capabilities
        )
        findings += audit_mmu(mmu, features)
        findings += audit_isolation(isolation, features)
        return DiagnosticReport(self.service, target, findings=findings)
