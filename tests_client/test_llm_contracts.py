# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""LLM contract tests, run by the client suite without the GDB shim."""

import asyncio
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from pyGdbClient.llm.contracts import (
    ActionEffect,
    ActionPlan,
    ContextFact,
    MemoryMapContext,
    MemoryRegion,
    PlannedAction,
    PolicyDecision,
    PolicyOutcome,
    TargetContext,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from pyGdbClient.llm.provider import (
    AuthenticationState,
    LlmProvider,
    ProviderCancelledError,
    ProviderError,
    TextDelta,
    ToolCallProposed,
    ToolResults,
    TurnFinished,
    UserPrompt,
)
from pyGdbClient.llm.scripted import ScriptedProvider


def _context() -> TargetContext:
    return TargetContext(
        context_id="target-1:snapshot-1",
        mcu=ContextFact("Example MCU", "configuration"),
        revision=ContextFact(None, "configuration", "Revision not supplied"),
        architecture=ContextFact("armv8-m", "toolkit"),
        debug_transport=ContextFact("swd", "configuration"),
        ocd=ContextFact("pyocd", "server.status"),
        core=ContextFact("0", "target.core"),
        memory_map=MemoryMapContext(
            (MemoryRegion(0x20000000, 0x20001000, "ram", "configuration"),),
            "toolkit",
        ),
        svd_file=ContextFact(None, "svd.peripherals", "No SVD loaded"),
    )


def test_context_retains_provenance_and_unavailable_values() -> None:
    """Unknown metadata stays unknown; memory ranges carry no access permission."""
    context = _context()
    assert context.schema_version == 1
    assert context.revision.value is None
    assert context.revision.unavailable_reason == "Revision not supplied"
    assert context.debug_transport.source == "configuration"
    assert context.memory_map.regions[0].end == 0x20001000
    assert MemoryMapContext((), "configuration").unavailable_reason is None
    missing = MemoryMapContext((), "toolkit", "No confirmed memory map")
    assert missing.unavailable_reason == "No confirmed memory map"
    with pytest.raises(ValueError, match="require an ID"):
        replace(context, context_id="")


@pytest.mark.parametrize(
    ("value", "source", "reason"),
    [
        (None, "toolkit", None),
        (None, "toolkit", " "),
        ("MCU", "", None),
        (" ", "toolkit", None),
        ("MCU", "toolkit", "Unavailable"),
    ],
)
def test_context_facts_reject_ambiguous_evidence(
    value: str | None, source: str, reason: str | None
) -> None:
    """Known and unavailable values cannot be confused."""
    with pytest.raises(ValueError):
        ContextFact(value, source, reason)


@pytest.mark.parametrize(
    ("start", "end"), [(-1, 1), (0, 0), (2, 1), (0, (1 << 64) + 1), (False, 1)]
)
def test_memory_regions_reject_invalid_bounds(start: int, end: int) -> None:
    """Ranges use the same half-open 64-bit address domain as the toolkit."""
    with pytest.raises(ValueError, match="memory region"):
        MemoryRegion(start, end, "unknown", "test")


def test_memory_map_rejects_missing_or_contradictory_evidence() -> None:
    """Unavailable maps cannot simultaneously advertise usable region metadata."""
    region = _context().memory_map.regions[0]
    with pytest.raises(ValueError):
        MemoryMapContext((), "")
    with pytest.raises(ValueError):
        MemoryMapContext((region,), "toolkit", "Unavailable")
    with pytest.raises(ValueError):
        MemoryMapContext((), "toolkit", "")


def test_plan_and_policy_contracts_do_not_grant_approval() -> None:
    """Plans default to unknown effects and decisions require explanations."""
    call = ToolCall("call-1", "gdb.command", {"command": "info registers"})
    action = PlannedAction(call, "Inspect core state")
    plan = ActionPlan("plan-1", _context().context_id, "Inspect the stopped core", (action,))
    assert action.effects == (ActionEffect.UNKNOWN,)
    assert plan.target_context_id == _context().context_id
    decision = PolicyDecision(PolicyOutcome.REQUIRE_APPROVAL, ("Opaque command",))
    assert decision.outcome == PolicyOutcome.REQUIRE_APPROVAL
    with pytest.raises(ValueError, match="at least one step"):
        replace(plan, steps=())
    with pytest.raises(ValueError, match="distinct"):
        replace(plan, steps=(action, action))
    for reasons in ((), ("",), (" ",)):
        with pytest.raises(ValueError, match="reasons"):
            PolicyDecision(PolicyOutcome.ALLOW, reasons)
    with pytest.raises(ValueError, match="call ID"):
        ToolCall("", "gdb.command", {})


def test_scripted_provider_supports_proposals_results_and_isolated_sessions() -> None:
    """The same provider interface supports streaming and tool-result feedback."""

    async def exercise() -> None:
        call = ToolCall("call-1", "target.status", {})
        provider: LlmProvider = ScriptedProvider(
            (
                (TextDelta("Inspecting"), ToolCallProposed(call), TurnFinished("tool_calls")),
                (TextDelta("Target is stopped"), TurnFinished()),
            )
        )
        status = await provider.status()
        assert status.available
        assert status.authentication == AuthenticationState.NOT_REQUIRED
        tools = (ToolDefinition("target.status", "Get known state", {"type": "object"}),)
        first = await provider.open_session("session-1", _context(), tools)
        second = await provider.open_session("session-2", _context(), tools)
        prompt = UserPrompt("Explain target state")
        events = [event async for event in first.stream(prompt)]
        assert events == [
            TextDelta("Inspecting"),
            ToolCallProposed(call),
            TurnFinished("tool_calls"),
        ]
        proposal = events[1]
        assert isinstance(proposal, ToolCallProposed)
        proposal.call.arguments["changed"] = True
        fresh_events = [event async for event in second.stream(prompt)]
        assert fresh_events[1] == ToolCallProposed(call)
        feedback = ToolResults((ToolResult("call-1", '{"state":"stopped"}'),))
        assert [event async for event in first.stream(feedback)] == [
            TextDelta("Target is stopped"),
            TurnFinished(),
        ]
        with pytest.raises(ProviderError, match="no remaining turns"):
            _ = [event async for event in first.stream(prompt)]
        await first.close()
        await first.close()
        with pytest.raises(ProviderError, match="closed"):
            _ = [event async for event in first.stream(prompt)]
        await second.close()

    asyncio.run(exercise())


def test_active_turns_can_be_cancelled_but_not_overlapped() -> None:
    """Cancellation is explicit and does not fabricate successful completion."""

    async def exercise() -> None:
        provider = ScriptedProvider(((TextDelta("First"), TextDelta("Next"), TurnFinished()),))
        session = await provider.open_session("session-1", _context(), ())
        stream = session.stream(UserPrompt("Inspect"))
        assert await anext(stream) == TextDelta("First")
        with pytest.raises(ProviderError, match="already streaming"):
            await anext(session.stream(UserPrompt("Overlap")))
        await session.cancel()
        with pytest.raises(ProviderCancelledError, match="cancelled"):
            await anext(stream)
        await session.close()

    asyncio.run(exercise())


def test_close_cancels_active_streams_and_idle_cancel_preserves_next_turn() -> None:
    """Closing releases the stream; an idle cancel does not poison future work."""

    async def exercise() -> None:
        provider = ScriptedProvider(((TextDelta("First"), TurnFinished()),))
        session = await provider.open_session("session-1", _context(), ())
        await session.cancel()
        stream = session.stream(UserPrompt("Inspect"))
        assert await anext(stream) == TextDelta("First")
        await session.close()
        with pytest.raises(ProviderCancelledError):
            await anext(stream)

    asyncio.run(exercise())


def test_scripted_sessions_retain_inputs_without_sharing_mutable_payloads() -> None:
    """Recorded inputs remain independent of changes made by the caller."""

    async def exercise() -> None:
        from pyGdbClient.llm.scripted import ScriptedSession

        message = ToolResults((ToolResult("call-1", "Observation", is_error=True),))
        session = ScriptedSession(
            "session-1", _context(), (), ((TextDelta("Noted"), TurnFinished()),)
        )
        assert [event async for event in session.stream(message)]
        assert session.inputs == [message]
        assert session.inputs[0] is not message
        await session.close()

    asyncio.run(exercise())


def test_scripted_provider_rejects_incomplete_or_inconsistent_turns() -> None:
    """A missing terminal event must never be treated as success."""
    call = ToolCallProposed(ToolCall("call-1", "target.status", {}))
    for turn in (
        (),
        (TextDelta("Incomplete"),),
        (TurnFinished(), TurnFinished()),
        (call, TurnFinished()),
        (TurnFinished("tool_calls"),),
        (call, call, TurnFinished("tool_calls")),
    ):
        with pytest.raises(ValueError):
            ScriptedProvider((turn,))

    async def exercise() -> None:
        with pytest.raises(ValueError, match="nonempty ID"):
            await ScriptedProvider(()).open_session("", _context(), ())

    asyncio.run(exercise())


def test_llm_modules_import_without_gdb_toolkit_or_copilot_sdk() -> None:
    """A fresh interpreter rejects forbidden imports, including from the launcher."""
    source = Path(__file__).resolve().parents[1] / "src"
    script = """
import importlib.abc
import sys

class RejectTargetDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"gdb", "pyGdbToolkit", "copilot"}:
            raise AssertionError("Forbidden import: " + fullname)
        return None

sys.meta_path.insert(0, RejectTargetDependencies())
sys.path.insert(0, sys.argv[1])
from pyGdbClient.llm import contracts, controller, copilot, provider, registry, rendering, scripted
import pyGdbClient.cli
assert scripted.ScriptedProvider.provider_id == "scripted"
assert registry.default_registry().ids == ("github-copilot",)
"""
    subprocess.run([sys.executable, "-I", "-c", script, str(source)], check=True)
