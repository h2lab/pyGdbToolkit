# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""The bounded agent loop routes every proposal through policy, approval and budgets."""

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict, replace
import json
from pathlib import Path
from typing import Any

import pytest

from pyGdbClient.llm.approval import ApprovalRequest, ApprovalResponse
from pyGdbClient.llm.confidentiality import Redactor
from pyGdbClient.llm.contracts import (
    ContextFact,
    MemoryMapContext,
    TargetContext,
    ToolCall,
    ToolResult,
)
from pyGdbClient.llm.gateway import ToolGateway
from pyGdbClient.llm.orchestrator import (
    AgentBudget,
    AgentEvent,
    AgentNotice,
    AgentOrchestrator,
    AgentPlan,
    AgentRunFinished,
    AgentText,
    AgentToolResults,
    OrchestratorError,
)
from pyGdbClient.llm.policy import ProtectionPolicy
from pyGdbClient.llm.provider import (
    ProviderCancelledError,
    ProviderError,
    ProviderEvent,
    ProviderInput,
    TextDelta,
    ToolCallProposed,
    ToolResults,
    TurnFinished,
    UserPrompt,
)
from pyGdbClient.llm.scripted import ScriptedProvider
from pyGdbClient.llm.store import ContextStore, EntryKind
from pyGdbClient.rpc import JsonRpcClient
from rpc_fakes import FakeRpcServer

type Step = ProviderEvent | Exception | str


def _context(context_id: str = "ctx-1", mcu: str = "test") -> TargetContext:
    fact = ContextFact("test", "configuration")
    return TargetContext(
        context_id,
        ContextFact(mcu, "configuration"),
        fact,
        fact,
        fact,
        fact,
        fact,
        MemoryMapContext((), "test"),
        fact,
    )


def _snapshot(context: TargetContext | None = None) -> dict[str, Any]:
    snapshot: dict[str, Any] = json.loads(json.dumps(asdict(context or _context())))
    return snapshot


def _mi() -> dict[str, Any]:
    return {"class": "done", "record": "done", "output": ["observed"]}


def _status(call_id: str = "s1") -> ToolCall:
    return ToolCall(call_id, "target_status", {})


def _read(call_id: str = "r1") -> ToolCall:
    return ToolCall(call_id, "memory_read", {"address": 0x20000000, "length": 4})


def _tools(*calls: ToolCall, text: str = "") -> tuple[Step, ...]:
    events: list[Step] = [TextDelta(text)] if text else []
    events.extend(ToolCallProposed(call) for call in calls)
    events.append(TurnFinished("tool_calls"))
    return tuple(events)


def _answer(text: str = "done") -> tuple[Step, ...]:
    return (TextDelta(text), TurnFinished())


class FakeSession:
    """Provider session able to emit invalid protocols, failures and hangs."""

    def __init__(self, *turns: tuple[Step, ...], session_id: str = "s-1") -> None:
        self.session_id = session_id
        self.turns = list(turns)
        self.inputs: list[ProviderInput] = []
        self.cancelled = 0
        self.closed = 0
        self.streaming = asyncio.Event()
        self._cancel = asyncio.Event()

    async def stream(self, message: ProviderInput) -> AsyncIterator[ProviderEvent]:
        self.inputs.append(message)
        self._cancel = asyncio.Event()
        for step in self.turns.pop(0):
            await asyncio.sleep(0)
            if self._cancel.is_set():
                raise ProviderCancelledError("cancelled")
            if isinstance(step, Exception):
                raise step
            if step == "hang":
                self.streaming.set()
                await asyncio.Event().wait()
            elif isinstance(step, str):
                raise AssertionError(step)
            else:
                yield step

    async def cancel(self) -> None:
        self.cancelled += 1
        self._cancel.set()

    async def close(self) -> None:
        self.closed += 1


class Requester:
    """Loopback-free RPC fake recording dispatched methods."""

    def __init__(self) -> None:
        self.context = _context()
        self.calls: list[str] = []
        self.block = False
        self.dispatched = asyncio.Event()
        self.responses: dict[str, Any] = {}
        self.context_failures = 0

    async def request(
        self, method: str, params: Mapping[str, Any] | None = None, timeout: float = 60.0
    ) -> Any:
        self.calls.append(method)
        if method == "target.context":
            if self.context_failures:
                self.context_failures -= 1
                raise OSError("link lost")
            return _snapshot(self.context)
        self.dispatched.set()
        if self.block:
            await asyncio.Event().wait()
        return self.responses.get(method, {"state": "stopped"})


class Harness:
    def __init__(
        self,
        session: Any,
        *,
        budget: AgentBudget | None = None,
        approve: bool | None = None,
        policy: ProtectionPolicy | None = None,
        store: ContextStore | None = None,
        client: Any = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.events: list[AgentEvent] = []
        self.requests: list[ApprovalRequest] = []
        self.client = client or Requester()

        async def approver(request: ApprovalRequest) -> ApprovalResponse:
            self.requests.append(request)
            return request.respond(approved=bool(approve))

        gateway = ToolGateway(
            self.client,
            policy or ProtectionPolicy(),
            approver=None if approve is None else approver,
        )
        self.agent = AgentOrchestrator(
            session,
            gateway,
            _context(),
            sink=self.events.append,
            budget=budget,
            store=store,
            context_client=self.client,
            redactor=redactor,
        )

    def of[T](self, kind: type[T]) -> list[T]:
        return [event for event in self.events if isinstance(event, kind)]


def _errors(message: ProviderInput) -> list[dict[str, Any]]:
    assert isinstance(message, ToolResults)
    return [json.loads(result.content) for result in message.results]


def test_text_turn_streams_to_sink_and_journal(tmp_path: Path) -> None:
    async def exercise() -> None:
        with ContextStore(tmp_path / "agent.db") as store:
            store.create_session("s-1", "fake", _context())
            session = FakeSession((TextDelta("Hel"), TextDelta("lo"), TurnFinished()))
            harness = Harness(session, store=store)
            outcome = await harness.agent.submit("Describe the target")
            assert outcome.reason == "completed"
            assert outcome.output_chars == 5
            assert [event.text for event in harness.of(AgentText)] == ["Hel", "lo"]
            assert isinstance(harness.events[-1], AgentRunFinished)
            assert session.inputs == [UserPrompt("Describe the target")]
            entries = store.history("s-1")
            assert [(entry.kind, entry.payload) for entry in entries] == [
                (EntryKind.USER, {"text": "Describe the target"}),
                (EntryKind.ASSISTANT, {"text": "Hello"}),
            ]

    asyncio.run(exercise())


def test_scripted_provider_metadata_round_trips_over_real_rpc() -> None:
    """Allowed metadata executes without approval; results return to the provider."""

    async def exercise() -> None:
        provider = ScriptedProvider(
            (
                (
                    TextDelta("Checking state"),
                    ToolCallProposed(_status()),
                    TurnFinished("tool_calls"),
                ),
                (TextDelta("Target is stopped"), TurnFinished()),
            )
        )
        session = await provider.open_session("s-1", _context(), ())
        async with FakeRpcServer(
            {"target.context": _snapshot(), "target.status": {"state": "stopped"}}
        ) as server:
            client = JsonRpcClient(server.url)
            await client.connect()
            try:
                harness = Harness(session, client=client)
                outcome = await harness.agent.submit("Is the core halted?")
                assert outcome.reason == "completed"
                assert (outcome.rounds, outcome.tool_calls) == (1, 1)
                assert [method for method, _ in server.requests] == [
                    "target.context",
                    "target.status",
                ]
            finally:
                await client.close()
        plan = harness.of(AgentPlan)[0].plan
        assert plan.rationale == "Checking state"
        assert plan.target_context_id == "ctx-1"
        results = harness.of(AgentToolResults)[0].results
        assert not results[0].is_error
        assert getattr(session, "inputs")[1] == ToolResults(results)

    asyncio.run(exercise())


@pytest.mark.parametrize("approve", [True, False])
def test_risky_round_requires_auditor_and_rejection_is_returned_to_model(approve: bool) -> None:
    async def exercise() -> None:
        session = FakeSession(_tools(_status(), _read()), _answer())
        harness = Harness(session, approve=approve)
        harness.client.responses["mi.execute"] = _mi()
        outcome = await harness.agent.submit("Read the stack")
        assert outcome.reason == "completed"
        assert len(harness.requests) == 1
        if approve:
            assert harness.client.calls[-1] == "mi.execute"
            assert not any(result.is_error for result in harness.of(AgentToolResults)[0].results)
        else:
            assert harness.client.calls == []
            errors = _errors(session.inputs[1])
            assert all(error["executed"] is False for error in errors)
            assert all("rejected" in error["error"] for error in errors)

    asyncio.run(exercise())


@pytest.mark.parametrize("configured", ["no-approver", "deny"])
def test_blocked_plans_are_explained_to_model_without_dispatch(configured: str) -> None:
    async def exercise() -> None:
        session = FakeSession(_tools(_read()), _answer())
        policy = ProtectionPolicy(denied_tools=("memory_read",) if configured == "deny" else ())
        harness = Harness(session, policy=policy)
        assert (await harness.agent.submit("Read")).reason == "completed"
        assert harness.client.calls == []
        error = _errors(session.inputs[1])[0]
        assert "blocked" in error["error"]
        assert (
            "prohibited" in error["error"]
            if configured == "deny"
            else "require_approval" in error["error"]
        )

    asyncio.run(exercise())


def test_invalid_call_prevents_whole_round() -> None:
    async def exercise() -> None:
        session = FakeSession(_tools(_status(), ToolCall("bad", "shell", {})), _answer())
        harness = Harness(session)
        assert (await harness.agent.submit("Go")).reason == "completed"
        assert harness.client.calls == []
        assert harness.of(AgentPlan) == []
        errors = _errors(session.inputs[1])
        assert "nothing executed" in errors[0]["error"]
        assert "unknown tool" in errors[1]["error"]

    asyncio.run(exercise())


def test_failed_operation_marks_remaining_calls_not_executed() -> None:
    async def exercise() -> None:
        session = FakeSession(_tools(_status("a"), _status("b")), _answer())
        harness = Harness(session)
        harness.client.responses["target.status"] = "not an object"
        await harness.agent.submit("Go")
        errors = _errors(session.inputs[1])
        assert errors[0]["outcome"] == "unknown"
        assert "stopped after a failed operation" in errors[1]["error"]
        assert harness.client.calls.count("target.status") == 1

    asyncio.run(exercise())


def test_context_change_blocks_chain_and_rebinds_later_plans(tmp_path: Path) -> None:
    async def exercise() -> None:
        with ContextStore(tmp_path / "agent.db") as store:
            store.create_session("s-1", "fake", _context())
            session = FakeSession(_tools(_status()), _tools(_status("s2")), _answer())
            harness = Harness(session, store=store)
            harness.client.context = _context("ctx-2", "other")
            assert (await harness.agent.submit("Go")).reason == "completed"
            assert "context changed" in _errors(session.inputs[1])[0]["error"]
            assert harness.agent.context.context_id == "ctx-2"
            assert store.get_session("s-1").context_id == "ctx-2"
            assert any("ctx-2" in notice.message for notice in harness.of(AgentNotice))
            assert harness.of(AgentPlan)[1].plan.target_context_id == "ctx-2"
            assert harness.client.calls.count("target.status") == 1
            kinds = [entry.kind for entry in store.history("s-1")]
            assert kinds.count(EntryKind.TOOL_CALL) == 2
            assert kinds.count(EntryKind.TOOL_RESULT) == 2

    asyncio.run(exercise())


@pytest.mark.parametrize("failures", [1, 2])
def test_transient_or_persistent_context_failure_keeps_snapshot(failures: int) -> None:
    async def exercise() -> None:
        session = FakeSession(_tools(_status()), _answer())
        harness = Harness(session)
        harness.client.context_failures = failures
        assert (await harness.agent.submit("Go")).reason == "completed"
        assert "cannot verify live target context" in _errors(session.inputs[1])[0]["error"]
        assert harness.agent.context == _context()
        notices = [notice.message for notice in harness.of(AgentNotice)]
        assert notices == (["Cannot refresh target context: link lost"] if failures == 2 else [])
        assert "target.status" not in harness.client.calls

    asyncio.run(exercise())


def test_round_budget_requests_final_answer_without_tools() -> None:
    async def exercise() -> None:
        session = FakeSession(_tools(_status("a")), _tools(_status("b")), _answer("summary"))
        harness = Harness(session, budget=AgentBudget(max_tool_rounds=1))
        outcome = await harness.agent.submit("Go")
        assert outcome.reason == "budget_exhausted"
        assert (outcome.rounds, outcome.tool_calls) == (2, 1)
        assert harness.client.calls.count("target.status") == 1
        assert "budget exhausted" in _errors(session.inputs[2])[0]["error"]
        assert not harness.agent.requires_new_session

    asyncio.run(exercise())


def test_insisting_provider_after_budget_requires_new_session() -> None:
    async def exercise() -> None:
        session = FakeSession(_tools(_status("a")), _tools(_status("b")), _tools(_status("c")))
        harness = Harness(session, budget=AgentBudget(max_tool_rounds=1))
        outcome = await harness.agent.submit("Go")
        assert outcome.reason == "budget_exhausted"
        assert "new session" in outcome.message
        assert harness.agent.requires_new_session
        with pytest.raises(OrchestratorError, match="new session"):
            await harness.agent.submit("Again")

    asyncio.run(exercise())


def test_total_and_per_round_call_budgets() -> None:
    async def exercise() -> None:
        session = FakeSession(
            _tools(_status("a"), _status("b"), _status("c")),
            _tools(_status("d"), _status("e")),
            _answer(),
        )
        budget = AgentBudget(max_tool_calls=3, max_calls_per_round=2)
        harness = Harness(session, budget=budget)
        outcome = await harness.agent.submit("Go")
        assert "too many calls" in _errors(session.inputs[1])[0]["error"]
        assert harness.client.calls.count("target.status") == 2
        assert outcome.reason == "completed"
        assert outcome.tool_calls == 2

    asyncio.run(exercise())


def test_output_budget_cancels_provider_turn() -> None:
    async def exercise() -> None:
        session = FakeSession((TextDelta("12345"), TextDelta("678"), TurnFinished()))
        harness = Harness(session, budget=AgentBudget(max_output_chars=6))
        outcome = await harness.agent.submit("Go")
        assert outcome.reason == "budget_exhausted"
        assert session.cancelled == 1
        assert [event.text for event in harness.of(AgentText)] == ["12345"]

    asyncio.run(exercise())


def test_turn_timeout_cancels_provider() -> None:
    async def exercise() -> None:
        session = FakeSession((TextDelta("x"), "hang"), _answer())
        harness = Harness(session, budget=AgentBudget(turn_timeout=0.01))
        outcome = await harness.agent.submit("Go")
        assert outcome.reason == "timeout"
        assert session.cancelled == 1
        assert (await harness.agent.submit("Again")).reason == "completed"

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("turn", "message"),
    [
        ((TextDelta("x"),), "without TurnFinished"),
        ((TurnFinished(), TextDelta("x")), "after TurnFinished"),
        ((ToolCallProposed(_status()), TurnFinished()), "does not match"),
        ((TurnFinished("tool_calls"),), "does not match"),
        (
            (ToolCallProposed(_status()), ToolCallProposed(_status()), TurnFinished("tool_calls")),
            "reused",
        ),
        ((ProviderError("network down"),), "network down"),
        ((ProviderCancelledError("remote abort"),), "remote abort"),
    ],
)
def test_provider_protocol_violations_stop_without_execution(
    turn: tuple[Step, ...], message: str
) -> None:
    async def exercise() -> None:
        harness = Harness(FakeSession(turn))
        outcome = await harness.agent.submit("Go")
        assert outcome.reason == "provider_error"
        assert message in outcome.message
        assert harness.client.calls == []
        assert not harness.agent.requires_new_session

    asyncio.run(exercise())


def test_unknown_provider_event_is_rejected() -> None:
    async def exercise() -> None:
        harness = Harness(FakeSession((UserPrompt("injected"),)))  # type: ignore[arg-type]
        assert (await harness.agent.submit("Go")).message.endswith("unknown event")

    asyncio.run(exercise())


def test_cancel_during_streaming_keeps_session_usable(tmp_path: Path) -> None:
    async def exercise() -> None:
        with ContextStore(tmp_path / "agent.db") as store:
            store.create_session("s-1", "fake", _context())
            session = FakeSession((TextDelta("partial"), "hang"), _answer("next"))
            harness = Harness(session, store=store)
            task = asyncio.create_task(harness.agent.submit("Go"))
            await session.streaming.wait()
            assert harness.agent.running
            await harness.agent.cancel()
            outcome = await task
            assert outcome.reason == "cancelled"
            assert session.cancelled == 1
            assert not harness.agent.running
            assert not harness.agent.requires_new_session
            assert (await harness.agent.submit("Again")).reason == "completed"
            entries = store.history("s-1")
            assert (EntryKind.ASSISTANT, {"text": "partial"}) in [
                (entry.kind, entry.payload) for entry in entries
            ]
            assert any(entry.kind is EntryKind.ERROR for entry in entries)

    asyncio.run(exercise())


def test_cancel_during_dispatch_reports_uncertain_call_and_requires_new_session() -> None:
    async def exercise() -> None:
        session = FakeSession(_tools(_status("a"), _status("b")), _answer())
        harness = Harness(session)
        harness.client.block = True
        task = asyncio.create_task(harness.agent.submit("Go"))
        await harness.client.dispatched.wait()
        await harness.agent.cancel()
        await harness.agent.cancel()
        outcome = await task
        assert outcome.reason == "cancelled"
        assert "new session" in outcome.message
        assert harness.agent.requires_new_session
        assert any("a may still complete" in notice.message for notice in harness.of(AgentNotice))
        assert harness.client.calls == ["target.context", "target.status"]
        assert asyncio.current_task() is not None

    asyncio.run(exercise())


def test_external_cancellation_propagates() -> None:
    async def exercise() -> None:
        session = FakeSession(("hang",))
        harness = Harness(session)
        task = asyncio.create_task(harness.agent.submit("Go"))
        await session.streaming.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not harness.agent.running

    asyncio.run(exercise())


def test_cancel_after_completion_does_not_cancel_caller() -> None:
    async def exercise() -> None:
        harness = Harness(FakeSession(_answer()))
        await harness.agent.submit("Go")
        await harness.agent.cancel()
        await asyncio.sleep(0)
        assert asyncio.current_task() is not None

    asyncio.run(exercise())


def test_concurrent_requests_and_invalid_prompts_are_rejected() -> None:
    async def exercise() -> None:
        session = FakeSession(("hang",))
        harness = Harness(session, budget=AgentBudget(max_prompt_chars=5))
        with pytest.raises(ValueError, match="non-empty"):
            await harness.agent.submit("  ")
        with pytest.raises(ValueError, match="exceeds"):
            await harness.agent.submit("too long")
        task = asyncio.create_task(harness.agent.submit("Go"))
        await session.streaming.wait()
        with pytest.raises(OrchestratorError, match="already running"):
            await harness.agent.submit("Go")
        await harness.agent.close()
        assert (await task).reason == "cancelled"
        await harness.agent.close()
        assert session.closed == 1
        with pytest.raises(OrchestratorError, match="closed"):
            await harness.agent.submit("Go")

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "change",
    [
        {"max_tool_rounds": 0},
        {"max_tool_calls": True},
        {"max_calls_per_round": 1.5},
        {"max_output_chars": -1},
        {"turn_timeout": 0},
        {"turn_timeout": float("inf")},
        {"turn_timeout": True},
    ],
)
def test_budget_validation(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(AgentBudget(), **change)


def test_redactor_masks_disclosures_but_not_sink_or_journal(tmp_path: Path) -> None:
    async def exercise() -> None:
        with ContextStore(tmp_path / "agent.db") as store:
            store.create_session("s-1", "fake", _context())
            session = FakeSession(_tools(_status()), _answer())
            harness = Harness(session, store=store, redactor=Redactor())
            harness.client.responses["target.status"] = {"state": "password=hunter2"}
            outcome = await harness.agent.submit("Unlock with secret=opensesame")
            assert outcome.reason == "completed"
            prompt, results = session.inputs
            assert prompt == UserPrompt("Unlock with [REDACTED:credential-assignment]")
            assert isinstance(results, ToolResults)
            assert "hunter2" not in results.results[0].content
            assert "[REDACTED:credential-assignment]" in results.results[0].content
            observed = harness.of(AgentToolResults)[0].results[0]
            assert "hunter2" in observed.content
            notices = [notice.message for notice in harness.of(AgentNotice)]
            assert notices.count("Redacted 1 value(s) before disclosure to the provider") == 2
            journal = {entry.kind: entry.payload for entry in store.history("s-1")}
            assert journal[EntryKind.USER] == {"text": "Unlock with secret=opensesame"}
            assert "hunter2" in str(journal[EntryKind.TOOL_RESULT]["content"])

    asyncio.run(exercise())


def test_recollection_prefixes_only_the_first_prompt(tmp_path: Path) -> None:
    async def exercise() -> None:
        with ContextStore(tmp_path / "agent.db") as store:
            store.create_session("s-1", "fake", _context())
            session = FakeSession(_answer(), _answer())
            harness = Harness(session, store=store)
            await harness.agent.submit("first", recollection="Earlier: clock at 8 MHz")
            await harness.agent.submit("second")
            assert session.inputs == [
                UserPrompt("Earlier: clock at 8 MHz\n\nCurrent auditor request:\nfirst"),
                UserPrompt("second"),
            ]
            kinds = [(entry.kind, entry.payload) for entry in store.history("s-1")][:2]
            assert kinds == [
                (EntryKind.SUMMARY, {"text": "Earlier: clock at 8 MHz"}),
                (EntryKind.USER, {"text": "first"}),
            ]
            with pytest.raises(ValueError, match="recollection"):
                await harness.agent.submit("third", recollection=3)  # type: ignore[arg-type]

    asyncio.run(exercise())


def test_without_redactor_results_are_disclosed_verbatim() -> None:
    session = FakeSession(_tools(_status()), _answer())
    harness = Harness(session)
    harness.client.responses["target.status"] = {"state": "password=hunter2"}
    asyncio.run(harness.agent.submit("status"))
    results = session.inputs[1]
    assert isinstance(results, ToolResults)
    assert all(isinstance(result, ToolResult) for result in results.results)
    assert "hunter2" in results.results[0].content
    assert not harness.of(AgentNotice)
