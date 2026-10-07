# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Initial AArch64 security-audit context behind the portable diagnostic API."""

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
from .target import AArch64TargetDescription


class AArch64SecurityAuditor:
    """Observe CPU context without claiming that security controls were audited."""

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
        midr = None if registers is None else registers.read_first(("midr_el1", "MIDR_EL1"))
        if midr is None:
            identity_detail = "MIDR_EL1 is not exposed or readable; CPU identity is unknown."
        else:
            identity = decode_midr(midr & 0xFFFFFFFF)
            identity_detail = (
                f"{identity.core_name} {identity.rnp_revision}; "
                f"MIDR_EL1[31:0]=0x{midr & 0xFFFFFFFF:08X}. "
                "The CPU revision is not an ARMv8-A architecture minor version."
            )
        findings = (
            DiagnosticFinding(
                "Scope",
                DiagnosticSeverity.INFO,
                "AArch64 audit coverage is limited",
                "Only CPU identity and the current execution level are observed. "
                "Memory protection, isolation and optional security extensions are not audited. "
                "This report does not establish a secure configuration.",
            ),
            DiagnosticFinding(
                "CPU context", DiagnosticSeverity.INFO, "CPU identity", identity_detail
            ),
            DiagnosticFinding(
                "CPU context",
                DiagnosticSeverity.INFO,
                "Current exception level",
                self._exception_level_detail(registers),
            ),
            DiagnosticFinding(
                "Architecture",
                DiagnosticSeverity.INFO,
                "ARMv8-A minor version is undetermined",
                "GDB AArch64 metadata and MIDR_EL1 do not establish an ARMv8.x-A version. "
                "Architectural feature decoding is not yet part of this audit.",
            ),
        )
        return DiagnosticReport(self.service, target, findings=findings)

    @staticmethod
    def _exception_level_detail(registers: DiagnosticRegisterReader | None) -> str:
        """Use CurrentEL or a valid AArch64 PSTATE mode without guessing hidden state."""
        if registers is None:
            return "No runtime register reader is available; the current EL is unknown."
        current_el = registers.read_first(("currentel", "CurrentEL"))
        if current_el is not None:
            if current_el in (0, 4, 8, 12):
                return f"EL{current_el >> 2}, observed through CurrentEL."
            return "CurrentEL has an invalid encoding; the current EL is unknown."
        pstate = registers.read_first(("pstate", "cpsr"))
        if pstate is None:
            return "CurrentEL and PSTATE are not exposed or readable; the current EL is unknown."
        mode = pstate & 0x1F
        if mode not in (0, 4, 5, 8, 9, 12, 13):
            return "PSTATE does not identify a valid AArch64 mode; the current EL is unknown."
        return f"EL{mode >> 2}, observed through GDB PSTATE.M[3:2]."
