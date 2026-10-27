# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Data exchanged across the provider, orchestrator, and target-policy boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]


@dataclass(frozen=True)
class ContextFact:
    """A sourced value, or an explicit explanation of its unavailability."""

    value: str | None
    source: str
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        """Require provenance and distinguish unavailable from known information."""
        if not self.source.strip():
            raise ValueError("context facts require a source")
        if self.value is None:
            if not self.unavailable_reason or not self.unavailable_reason.strip():
                raise ValueError("unavailable context facts require a reason")
        elif not self.value.strip() or self.unavailable_reason is not None:
            raise ValueError("known context facts require a value and no unavailable reason")


@dataclass(frozen=True)
class MemoryRegion:
    """A declared half-open range; not permission or proof of safe access."""

    start: int
    end: int
    kind: str
    source: str
    name: str = ""
    protection: str = "unknown"

    def __post_init__(self) -> None:
        """Reject invalid address ranges and missing provenance."""
        if (
            isinstance(self.start, bool)
            or isinstance(self.end, bool)
            or not 0 <= self.start < self.end <= 1 << 64
        ):
            raise ValueError("memory region must satisfy 0 <= start < end <= 2**64")
        if not self.kind.strip() or not self.source.strip():
            raise ValueError("memory regions require a kind and source")


@dataclass(frozen=True)
class MemoryMapContext:
    """Available declared regions, or an explicit reason no map is available."""

    regions: tuple[MemoryRegion, ...]
    source: str
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        """Keep missing maps distinct from available maps, including empty maps."""
        if not self.source.strip():
            raise ValueError("memory maps require a source")
        if self.unavailable_reason is not None:
            if self.regions or not self.unavailable_reason.strip():
                raise ValueError("unavailable memory maps require a reason and no regions")


@dataclass(frozen=True)
class TargetContext:
    """Versioned target snapshot; paths refer to the server, not the client."""

    context_id: str
    mcu: ContextFact
    revision: ContextFact
    architecture: ContextFact
    debug_transport: ContextFact
    ocd: ContextFact
    core: ContextFact
    memory_map: MemoryMapContext
    svd_file: ContextFact
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        """Require a snapshot identity and a supported schema version."""
        if not self.context_id.strip() or self.schema_version != 1:
            raise ValueError("target contexts require an ID and schema version 1")


@dataclass(frozen=True)
class ToolDefinition:
    """An advertised tool schema, without an executable callback."""

    name: str
    description: str
    input_schema: JsonObject


@dataclass(frozen=True)
class ToolCall:
    """An untrusted proposed invocation, never an authorization to execute."""

    call_id: str
    name: str
    arguments: JsonObject

    def __post_init__(self) -> None:
        """Require identifiers for tool lookup and result correlation."""
        if not self.call_id.strip() or not self.name.strip():
            raise ValueError("tool calls require a call ID and tool name")


@dataclass(frozen=True)
class ToolResult:
    """A correlated observation or explicit execution failure."""

    call_id: str
    content: str
    is_error: bool = False


class ActionEffect(StrEnum):
    """Possible effects to display; provider claims are not policy authority."""

    OBSERVE = "observe"
    READ_MEMORY = "read_memory"
    WRITE_MEMORY = "write_memory"
    EXECUTION_CONTROL = "execution_control"
    PERSISTENT_CHANGE = "persistent_change"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PlannedAction:
    """One proposed step and its stated purpose and possible effects."""

    call: ToolCall
    purpose: str
    effects: tuple[ActionEffect, ...] = (ActionEffect.UNKNOWN,)


@dataclass(frozen=True)
class ActionPlan:
    """Ordered proposed steps bound to a specific target-context snapshot."""

    plan_id: str
    target_context_id: str
    rationale: str
    steps: tuple[PlannedAction, ...]

    def __post_init__(self) -> None:
        """Require identifiable, nonempty plans with unambiguous step correlation."""
        if not self.plan_id.strip() or not self.target_context_id.strip() or not self.steps:
            raise ValueError("plans require IDs and at least one step")
        call_ids = [step.call.call_id for step in self.steps]
        if any(not call_id.strip() for call_id in call_ids) or len(set(call_ids)) != len(call_ids):
            raise ValueError("plan steps require distinct, nonempty call IDs")


class PolicyOutcome(StrEnum):
    """Decisions made by local policy, not by the model."""

    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyDecision:
    """An explained policy assessment; this is not an auditor approval token."""

    outcome: PolicyOutcome
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        """Require an explanation for every outcome, including allowance."""
        if not self.reasons or any(not reason.strip() for reason in self.reasons):
            raise ValueError("policy decisions require nonempty reasons")


class TargetPolicy(Protocol):
    """Assess plans independently of provider prompts and proposed risk labels."""

    def evaluate(self, plan: ActionPlan, context: TargetContext) -> PolicyDecision:
        """Assess the complete plan against the current target snapshot."""
        ...
