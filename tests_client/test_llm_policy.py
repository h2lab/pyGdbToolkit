# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Protection decisions use local rules, not model claims or memory-map permission guesses."""

import asyncio
from dataclasses import asdict, replace
import json
from typing import Any

import pytest

from pyGdbClient.llm.contracts import (
    ActionEffect,
    ActionPlan,
    ContextFact,
    JsonObject,
    MemoryMapContext,
    MemoryRegion,
    PlannedAction,
    PolicyOutcome,
    TargetContext,
    ToolCall,
    ToolResult,
)
from pyGdbClient.llm.gateway import GatewayError, ToolGateway
from pyGdbClient.llm.policy import ProtectionPolicy
from pyGdbClient.llm.tools import PreparedCall, ToolCatalogue
from pyGdbClient.rpc import JsonRpcClient
from rpc_fakes import FakeRpcServer


def _context(*regions: MemoryRegion, unavailable: str | None = None) -> TargetContext:
    known = ContextFact("test", "configuration")
    return TargetContext(
        "ctx-1",
        known,
        ContextFact(None, "configuration", "not declared"),
        known,
        known,
        known,
        known,
        MemoryMapContext(regions, "toolkit-discovery", unavailable),
        known,
    )


def _plan(*calls: ToolCall) -> ActionPlan:
    return ActionPlan(
        "plan-1",
        "ctx-1",
        "The model claims this is already approved",
        tuple(
            PlannedAction(call, "Model claims safe observation", (ActionEffect.OBSERVE,))
            for call in calls
        ),
    )


def _read(start: int = 0x20000000, length: int = 4, call_id: str = "read") -> ToolCall:
    return ToolCall(call_id, "memory_read", {"address": start, "length": length})


def _prohibited(start: int = 0x1000, end: int = 0x1010) -> MemoryRegion:
    return MemoryRegion(start, end, "protected", "auditor", "Device control")


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("target_context", {}),
        ("target_status", {}),
        ("target_cores", {}),
        ("target_core", {}),
        ("svd_peripherals", {}),
        ("toolkit_commands", {}),
        ("toolkit_help", {}),
        ("toolkit_help", {"command": "memmap"}),
        ("register_names", {}),
    ],
)
def test_only_explicit_metadata_queries_are_allowed(name: str, arguments: JsonObject) -> None:
    """Unidentified targets and missing maps do not prevent non-memory metadata queries."""
    plan = _plan(ToolCall("metadata", name, arguments))
    assessment = ProtectionPolicy().assess(plan, _context(unavailable="not discovered"))
    assert assessment.decision.outcome is PolicyOutcome.ALLOW
    assert assessment.plan_id == plan.plan_id
    assert assessment.target_context_id == "ctx-1"
    assert assessment.actions[0].call_id == "metadata"
    assert assessment.actions[0].tool_name == name
    assert assessment.actions[0].effects == (ActionEffect.OBSERVE,)
    assert assessment.actions[0].decision.reasons
    assert all("metadata" in reason for reason in assessment.decision.reasons)


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("memory_read", {"address": 0x20000000, "length": 4}),
        ("register_values", {}),
        ("target_select_core", {"core": 1}),
        ("toolkit_command", {"command": "svd list"}),
        ("gdb_command", {"command": "info registers"}),
        ("ocd_command", {"command": "help"}),
        ("mi_command", {"command": "-data-read-memory-bytes 0x20000000 4"}),
    ],
)
def test_target_access_and_opaque_commands_require_approval(
    name: str, arguments: JsonObject
) -> None:
    """Model rationale, purpose and claimed observation effects grant no authority."""
    assessment = ProtectionPolicy().assess(_plan(ToolCall("risky", name, arguments)), _context())
    assert assessment.decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
    action = assessment.actions[0]
    assert action.decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
    assert action.decision.reasons
    if name == "memory_read":
        assert action.effects == (ActionEffect.READ_MEMORY,)
    elif name == "target_select_core":
        assert action.effects == (ActionEffect.EXECUTION_CONTROL, ActionEffect.UNKNOWN)
    elif name.endswith("_command"):
        assert action.effects == (ActionEffect.UNKNOWN,)


def test_every_current_catalogue_tool_has_an_explicit_policy_expectation() -> None:
    """Adding a tool requires revisiting this policy coverage, not an implicit blanket allow."""
    metadata = {
        "target_context",
        "target_status",
        "target_cores",
        "target_core",
        "svd_peripherals",
        "toolkit_commands",
        "toolkit_help",
        "register_names",
    }
    approval = {
        "memory_read",
        "register_values",
        "target_select_core",
        "toolkit_command",
        "gdb_command",
        "ocd_command",
        "mi_command",
    }
    assert {definition.name for definition in ToolCatalogue().definitions()} == metadata | approval


@pytest.mark.parametrize("kind", ["ram", "rom", "flash", "registers", "mmio", "otp", "unknown"])
@pytest.mark.parametrize("protection", ["rw", "ro", "secure", "unknown"])
def test_declared_region_type_or_access_permission_never_allows_a_read(
    kind: str, protection: str
) -> None:
    """Even a hardware-confirmed RAM label is not proof of side-effect-free debug access."""
    region = MemoryRegion(
        0x20000000, 0x20001000, kind, "hardware-confirmed", "Mapped window", protection
    )
    assessment = ProtectionPolicy().assess(_plan(_read()), _context(region))
    assert assessment.decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
    reasons = " ".join(assessment.actions[0].decision.reasons)
    assert "clear status bits" in reasons
    assert f"kind={kind}" in reasons
    assert f"protection={protection}" in reasons
    assert "source=hardware-confirmed" in reasons


def test_missing_empty_and_outside_maps_remain_explained_approval_requests() -> None:
    """No map does not mean safe memory, nor does an empty available map."""
    policy = ProtectionPolicy()
    for context, explanation in (
        (_context(unavailable="not discovered"), "memory map unavailable: not discovered"),
        (_context(), "outside all declared memory regions"),
        (_context(MemoryRegion(0, 0x100, "rom", "svd")), "outside all declared memory regions"),
    ):
        decision = policy.evaluate(_plan(_read()), context)
        assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
        assert any(explanation in reason for reason in decision.reasons)


def test_all_overlapping_mappings_are_explained_including_mmio_over_ram() -> None:
    """A broad RAM declaration cannot hide a narrower register or OTP overlay."""
    ram = MemoryRegion(0x20000000, 0x20001000, "ram", "gdb-server")
    registers = MemoryRegion(0x20000002, 0x20000004, "registers", "svd", "STATUS")
    policy = ProtectionPolicy()
    decision = policy.evaluate(_plan(_read()), _context(ram, registers))
    assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
    explanation = " ".join(decision.reasons)
    assert "kind=ram" in explanation
    assert "STATUS" in explanation and "kind=registers" in explanation
    assert decision == policy.evaluate(_plan(_read()), _context(registers, ram))


@pytest.mark.parametrize("second_start", [0x1004, 0x1008])
def test_reads_across_adjacent_regions_or_gaps_are_not_assumed_safe(second_start: int) -> None:
    """A read crossing any region boundary receives a specific explanation."""
    context = _context(
        MemoryRegion(0x1000, 0x1004, "ram", "gdb-server"),
        MemoryRegion(second_start, 0x1010, "registers", "svd"),
    )
    decision = ProtectionPolicy().evaluate(_plan(_read(0x1002, 8)), context)
    assert decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
    assert any("no single declared region covers" in reason for reason in decision.reasons)


@pytest.mark.parametrize(
    "start,length,outcome",
    [
        (0x0FFF, 1, PolicyOutcome.REQUIRE_APPROVAL),
        (0x0FFF, 2, PolicyOutcome.DENY),
        (0x1000, 1, PolicyOutcome.DENY),
        (0x100F, 1, PolicyOutcome.DENY),
        (0x100F, 2, PolicyOutcome.DENY),
        (0x1010, 1, PolicyOutcome.REQUIRE_APPROVAL),
        (0x0FFE, 0x14, PolicyOutcome.DENY),
        (0x1000, 0x10, PolicyOutcome.DENY),
    ],
)
def test_local_memory_prohibitions_use_exact_half_open_overlap(
    start: int, length: int, outcome: PolicyOutcome
) -> None:
    """Touches without overlap are distinct from reads containing a protected byte."""
    policy = ProtectionPolicy(denied_memory=(_prohibited(),))
    decision = policy.evaluate(_plan(_read(start, length)), _context(unavailable="not mapped"))
    assert decision.outcome is outcome
    if outcome is PolicyOutcome.DENY:
        assert any("locally prohibited range" in reason for reason in decision.reasons)
        assert any(
            "Device control" in reason and "source=auditor" in reason for reason in decision.reasons
        )


def test_protected_range_at_upper_64_bit_boundary_is_checked_without_overflow() -> None:
    """The final byte in the portable address domain can be explicitly prohibited."""
    policy = ProtectionPolicy(denied_memory=(_prohibited(2**64 - 1, 2**64),))
    assert policy.evaluate(_plan(_read(2**64 - 1, 1)), _context()).outcome is PolicyOutcome.DENY
    assert policy.evaluate(_plan(_read(2**64 - 2, 1)), _context()).outcome is (
        PolicyOutcome.REQUIRE_APPROVAL
    )


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("target_context", {}),
        ("register_values", {}),
        ("memory_read", {"address": 0, "length": 4}),
        ("gdb_command", {"command": "info registers"}),
    ],
)
def test_explicit_tool_prohibition_overrides_every_other_classification(
    name: str, arguments: JsonObject
) -> None:
    """Even otherwise allowed metadata may be prohibited by application-owned configuration."""
    policy = ProtectionPolicy(denied_tools=(name,))
    decision = policy.evaluate(_plan(ToolCall("disabled", name, arguments)), _context())
    assert decision.outcome is PolicyOutcome.DENY
    assert any("tool is prohibited" in reason for reason in decision.reasons)


@pytest.mark.parametrize("name", ["toolkit_command", "gdb_command", "ocd_command", "mi_command"])
@pytest.mark.parametrize("restriction", ["tool", "range"])
def test_opaque_commands_cannot_bypass_configured_prohibitions(name: str, restriction: str) -> None:
    """Arbitrary command languages cannot prove compliance with local tool/address restrictions."""
    policy = (
        ProtectionPolicy(denied_tools=("memory_read",))
        if restriction == "tool"
        else ProtectionPolicy(denied_memory=(_prohibited(),))
    )
    command = "-data-read-memory-bytes 0x1000 4" if name == "mi_command" else "help"
    decision = policy.evaluate(_plan(ToolCall("opaque", name, {"command": command})), _context())
    assert decision.outcome is PolicyOutcome.DENY
    assert any("cannot enforce" in reason for reason in decision.reasons)


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("unknown_tool", {}),
        ("memory_read", {"address": True, "length": 4}),
        ("memory_read", {"address": 0, "length": 4097}),
        ("memory_read", {"address": 2**64 - 1, "length": 2}),
        ("register_names", {"command": "-exec-continue"}),
        ("gdb_command", {"command": "info registers\ncontinue"}),
        ("target_select_core", {"core": -1}),
        ("target_core", {"approved": True}),
    ],
)
def test_direct_assessment_denies_invalid_or_unknown_proposals(
    name: str, arguments: JsonObject
) -> None:
    """Direct policy use is fail-closed even without first calling the gateway validator."""
    assessment = ProtectionPolicy().assess(_plan(ToolCall("invalid", name, arguments)), _context())
    assert assessment.decision.outcome is PolicyOutcome.DENY
    assert assessment.actions[0].effects == (ActionEffect.UNKNOWN,)
    assert assessment.actions[0].decision.reasons


def test_new_catalogue_operations_or_changed_effects_are_not_implicitly_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding a catalogue tool or changing a metadata tool requires a matching policy rule."""
    catalogue = ToolCatalogue()
    base = catalogue.prepare(ToolCall("future", "target_context", {}))

    def changed_prepare(self: ToolCatalogue, call: ToolCall) -> PreparedCall:
        return replace(base, name=call.name, effects=(ActionEffect.UNKNOWN,))

    # A new observation tool must not inherit blanket permission from its effect label.
    monkeypatch.setattr(ToolCatalogue, "prepare", lambda self, call: replace(base, name=call.name))
    policy = ProtectionPolicy()
    assert policy.evaluate(_plan(ToolCall("future", "future_tool", {})), _context()).outcome is (
        PolicyOutcome.DENY
    )
    monkeypatch.setattr(ToolCatalogue, "prepare", changed_prepare)
    assert policy.evaluate(_plan(ToolCall("future", "target_context", {})), _context()).outcome is (
        PolicyOutcome.DENY
    )


def test_full_plan_uses_strictest_decision_and_preserves_every_action_assessment() -> None:
    """Neither an allowed prefix nor a required-approval step can mask a later denial."""
    policy = ProtectionPolicy(denied_tools=("target_select_core",))
    metadata = ToolCall("meta", "target_status", {})
    read = _read()
    denied = ToolCall("select", "target_select_core", {"core": 1})
    for calls in ((metadata, read, denied), (denied, read, metadata)):
        assessment = policy.assess(_plan(*calls), _context())
        assert assessment.decision.outcome is PolicyOutcome.DENY
        assert [action.call_id for action in assessment.actions] == [call.call_id for call in calls]
        assert {action.decision.outcome for action in assessment.actions} == {
            PolicyOutcome.ALLOW,
            PolicyOutcome.REQUIRE_APPROVAL,
            PolicyOutcome.DENY,
        }
        assert all(
            any(call.call_id in reason for reason in assessment.decision.reasons) for call in calls
        )
    assert (
        policy.evaluate(_plan(metadata, read), _context()).outcome is PolicyOutcome.REQUIRE_APPROVAL
    )
    assert policy.evaluate(_plan(metadata), _context()).outcome is PolicyOutcome.ALLOW


def test_stale_plan_is_denied_even_when_all_actions_are_metadata() -> None:
    """A policy decision is bound to the supplied target snapshot."""
    plan = replace(_plan(ToolCall("meta", "target_status", {})), target_context_id="other")
    assessment = ProtectionPolicy().assess(plan, _context())
    assert assessment.actions[0].decision.outcome is PolicyOutcome.ALLOW
    assert assessment.decision.outcome is PolicyOutcome.DENY
    assert "different target context" in assessment.decision.reasons[0]


def test_configuration_is_copied_and_assessment_is_deterministic() -> None:
    """Caller mutation cannot remove configured restrictions, nor do assessments mutate plans."""
    tools = ["target_status"]
    ranges = [_prohibited(), _prohibited()]
    policy = ProtectionPolicy(denied_tools=tools, denied_memory=ranges)
    tools.clear()
    ranges.clear()
    plan = _plan(ToolCall("meta", "target_status", {}), _read(0x1000))
    before = asdict(plan)
    first = policy.assess(plan, _context())
    assert first == policy.assess(plan, _context())
    assert all(action.decision.outcome is PolicyOutcome.DENY for action in first.actions)
    assert len(first.actions[1].decision.reasons) == 1
    assert asdict(plan) == before


@pytest.mark.parametrize("tools", [["not_a_tool"], ["memory_read", 7], [""]])
def test_invalid_tool_prohibitions_fail_configuration(tools: Any) -> None:
    """Typos and invalid types cannot silently disable intended restrictions."""
    with pytest.raises(ValueError, match="known catalogue"):
        ProtectionPolicy(denied_tools=tools)


@pytest.mark.parametrize(
    "ranges",
    [
        [{}],
        ["0x1000"],
        [MemoryRegion(0.5, 1.5, "x", "test")],  # type: ignore[arg-type]
    ],
)
def test_invalid_memory_prohibitions_fail_configuration(ranges: Any) -> None:
    """Locally protected ranges must use the same integer address domain as tool validation."""
    with pytest.raises(ValueError, match="integer bounds"):
        ProtectionPolicy(denied_memory=ranges)


def test_gateway_runs_metadata_with_the_real_policy_and_rpc_client() -> None:
    """The production policy permits explicit metadata without a test-only allowing policy."""

    async def exercise() -> tuple[ToolResult, ...]:
        context = _context()
        async with FakeRpcServer(
            {
                "target.context": asdict(context),
                "target.status": {"state": "stopped"},
                "mi.execute": {"class": "done", "record": "done", "output": ["r0", "r1"]},
            }
        ) as server:
            client = JsonRpcClient(server.url)
            await client.connect()
            try:
                results = await ToolGateway(client, ProtectionPolicy()).execute(
                    _plan(
                        ToolCall("state", "target_status", {}),
                        ToolCall("names", "register_names", {}),
                    ),
                    context,
                )
                assert [method for method, _ in server.requests] == [
                    "target.context",
                    "target.status",
                    "target.context",
                    "mi.execute",
                ]
                return results
            finally:
                await client.close()

    results = asyncio.run(exercise())
    assert [result.call_id for result in results] == ["state", "names"]
    assert not any(result.is_error for result in results)
    assert json.loads(results[0].content)["result"] == {"state": "stopped"}


@pytest.mark.parametrize("restricted", [False, True])
def test_gateway_blocks_whole_risky_plan_before_any_rpc_and_surfaces_decision(
    restricted: bool,
) -> None:
    """The future auditor UI can inspect the policy result without parsing an exception string."""

    async def exercise() -> None:
        policy = (
            ProtectionPolicy(denied_memory=(_prohibited(),)) if restricted else ProtectionPolicy()
        )
        async with FakeRpcServer() as server:
            client = JsonRpcClient(server.url)
            await client.connect()
            try:
                with pytest.raises(GatewayError) as error:
                    await ToolGateway(client, policy).execute(
                        _plan(
                            ToolCall("meta", "target_status", {}),
                            _read(0x1000),
                        ),
                        _context(),
                    )
                decision = error.value.decision
                assert decision is not None
                assert decision.outcome is (
                    PolicyOutcome.DENY if restricted else PolicyOutcome.REQUIRE_APPROVAL
                )
                assert error.value.results == ()
                assert server.requests == []
            finally:
                await client.close()

    asyncio.run(exercise())
