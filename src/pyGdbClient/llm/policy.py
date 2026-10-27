# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Deterministic target protection using validated tools and local restrictions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .contracts import (
    ActionEffect,
    ActionPlan,
    MemoryRegion,
    PolicyDecision,
    PolicyOutcome,
    TargetContext,
)
from .tools import PreparedCall, ToolCatalogue, ToolValidationError

_METADATA_TOOLS = frozenset(
    {
        "target_context",
        "target_status",
        "target_cores",
        "target_core",
        "svd_peripherals",
        "toolkit_commands",
        "toolkit_help",
        "register_names",
    }
)
_OPAQUE_TOOLS = frozenset({"toolkit_command", "gdb_command", "ocd_command", "mi_command"})
_PRECEDENCE = {
    PolicyOutcome.ALLOW: 0,
    PolicyOutcome.REQUIRE_APPROVAL: 1,
    PolicyOutcome.DENY: 2,
}


@dataclass(frozen=True)
class ActionAssessment:
    """Local effects and explained decision for one proposed invocation."""

    call_id: str
    tool_name: str
    effects: tuple[ActionEffect, ...]
    decision: PolicyDecision


@dataclass(frozen=True)
class PlanAssessment:
    """Ordered action assessments and their aggregate; never an approval token."""

    plan_id: str
    target_context_id: str
    actions: tuple[ActionAssessment, ...]
    decision: PolicyDecision


def _region_key(region: MemoryRegion) -> tuple[int, int, str, str, str, str]:
    return region.start, region.end, region.kind, region.source, region.name, region.protection


def _describe(region: MemoryRegion) -> str:
    label = f"{region.name}: " if region.name else ""
    return (
        f"{label}[0x{region.start:x}, 0x{region.end:x}), "
        f"kind={region.kind}, protection={region.protection}, source={region.source}"
    )


class ProtectionPolicy:
    """Require auditor approval for target accesses and deny explicit local restrictions.

    Configuration is application-owned, copied at construction, and never taken
    from prompts or tool arguments. Memory-map metadata cannot grant permission.
    """

    def __init__(
        self,
        *,
        denied_tools: Iterable[str] = (),
        denied_memory: Iterable[MemoryRegion] = (),
    ) -> None:
        """Bind optional tool prohibitions and half-open protected address ranges."""
        self._catalogue = ToolCatalogue()
        known = {definition.name for definition in self._catalogue.definitions()}
        tools = tuple(denied_tools)
        if any(not isinstance(tool, str) or tool not in known for tool in tools):
            raise ValueError("denied_tools must contain known catalogue tool names")
        ranges = tuple(denied_memory)
        if any(
            not isinstance(region, MemoryRegion)
            or isinstance(region.start, bool)
            or isinstance(region.end, bool)
            or not isinstance(region.start, int)
            or not isinstance(region.end, int)
            for region in ranges
        ):
            raise ValueError("denied_memory must contain MemoryRegion objects with integer bounds")
        self._denied_tools = frozenset(tools)
        self._denied_memory = tuple(sorted(set(ranges), key=_region_key))

    def evaluate(self, plan: ActionPlan, context: TargetContext) -> PolicyDecision:
        """Assess the complete plan with DENY > REQUIRE_APPROVAL > ALLOW precedence."""
        return self.assess(plan, context).decision

    def assess(self, plan: ActionPlan, context: TargetContext) -> PlanAssessment:
        """Explain each action from local tool data, independently of model effect claims."""
        actions: list[ActionAssessment] = []
        for step in plan.steps:
            try:
                prepared = self._catalogue.prepare(step.call)
            except ToolValidationError as error:
                actions.append(
                    ActionAssessment(
                        step.call.call_id,
                        step.call.name,
                        (ActionEffect.UNKNOWN,),
                        PolicyDecision(PolicyOutcome.DENY, (str(error),)),
                    )
                )
                continue
            actions.append(
                ActionAssessment(
                    prepared.call_id,
                    prepared.name,
                    prepared.effects,
                    self._assess_call(prepared, context),
                )
            )
        reasons = [
            f"{action.call_id} ({action.tool_name}): {reason}"
            for action in actions
            for reason in action.decision.reasons
        ]
        outcome = max((action.decision.outcome for action in actions), key=_PRECEDENCE.__getitem__)
        if plan.target_context_id != context.context_id:
            outcome = PolicyOutcome.DENY
            reasons.insert(
                0, "plan is bound to a different target context; a fresh plan is required"
            )
        return PlanAssessment(
            plan.plan_id,
            plan.target_context_id,
            tuple(actions),
            PolicyDecision(outcome, tuple(reasons)),
        )

    def _assess_call(self, call: PreparedCall, context: TargetContext) -> PolicyDecision:
        if call.name in self._denied_tools:
            return PolicyDecision(
                PolicyOutcome.DENY, ("tool is prohibited by local configuration",)
            )
        if call.name in _OPAQUE_TOOLS:
            if self._denied_tools or self._denied_memory:
                return PolicyDecision(
                    PolicyOutcome.DENY,
                    ("opaque commands cannot enforce configured tool/address prohibitions",),
                )
            return PolicyDecision(
                PolicyOutcome.REQUIRE_APPROVAL,
                (
                    "opaque command may write memory, resume/reset cores, modify flash/OTP "
                    "or debug protection, and execute host scripts or access host files",
                    "command text and model safety claims cannot establish safe effects",
                ),
            )
        if call.name == "memory_read":
            return self._memory_read(call, context)
        if call.name == "register_values":
            return PolicyDecision(
                PolicyOutcome.REQUIRE_APPROVAL,
                ("reading target register values is target access, not catalogue metadata",),
            )
        if call.name == "target_select_core":
            return PolicyDecision(
                PolicyOutcome.REQUIRE_APPROVAL,
                (
                    "attaching/selecting a core may halt it and changes the target context",
                    "later steps require a fresh context and a new assessment",
                ),
            )
        if call.name in _METADATA_TOOLS and call.effects == (ActionEffect.OBSERVE,):
            return PolicyDecision(
                PolicyOutcome.ALLOW,
                ("explicitly listed metadata query; no requested memory read or execution change",),
            )
        return PolicyDecision(PolicyOutcome.DENY, ("no protection rule exists for this operation",))

    def _memory_read(self, call: PreparedCall, context: TargetContext) -> PolicyDecision:
        arguments = call.tool_call().arguments
        start, length = arguments["address"], arguments["length"]
        if not isinstance(start, int) or not isinstance(length, int):
            return PolicyDecision(PolicyOutcome.DENY, ("memory read requires integer bounds",))
        end = start + length
        prohibited = [
            _describe(region)
            for region in self._denied_memory
            if start < region.end and region.start < end
        ]
        if prohibited:
            return PolicyDecision(
                PolicyOutcome.DENY,
                tuple(f"read overlaps locally prohibited range: {region}" for region in prohibited),
            )
        reasons = [
            f"read {length} bytes from [0x{start:x}, 0x{end:x}); auditor approval is required",
            "memory reads may clear status bits or consume FIFO data; RAM/ROM/flash labels "
            "and access protections are not evidence of side-effect-free access",
        ]
        memory_map = context.memory_map
        if memory_map.unavailable_reason is not None:
            reasons.append(f"memory map unavailable: {memory_map.unavailable_reason}")
        else:
            overlaps = sorted(
                (
                    region
                    for region in memory_map.regions
                    if start < region.end and region.start < end
                ),
                key=_region_key,
            )
            if not overlaps:
                reasons.append("read is outside all declared memory regions")
            else:
                reasons.extend(f"declared region: {_describe(region)}" for region in overlaps)
                if not any(region.start <= start and end <= region.end for region in overlaps):
                    reasons.append(
                        "no single declared region covers the read; it crosses region "
                        "boundaries or includes unmapped bytes"
                    )
        return PolicyDecision(PolicyOutcome.REQUIRE_APPROVAL, tuple(reasons))
