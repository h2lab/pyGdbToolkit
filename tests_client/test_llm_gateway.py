# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""A local policy and a fresh target snapshot gate every agent tool dispatch."""

import asyncio
from collections.abc import Mapping
from dataclasses import asdict, replace
import json
from typing import Any

import pytest

from pyGdbClient.llm.contracts import (
    ActionEffect,
    ActionPlan,
    ContextFact,
    MemoryMapContext,
    PlannedAction,
    PolicyDecision,
    PolicyOutcome,
    TargetContext,
    ToolCall,
)
from pyGdbClient.llm.gateway import (
    GatewayCancelledError,
    GatewayError,
    MAX_RESULT_BYTES,
    ToolGateway,
)
from pyGdbClient.llm.tools import ToolValidationError
from pyGdbClient.rpc import JsonRpcClient, RpcError
from rpc_fakes import FakeRpcServer, RpcFailure


def _context() -> TargetContext:
    known = ContextFact("test", "configuration")
    return TargetContext(
        "ctx-1",
        known,
        ContextFact(None, "configuration", "not declared"),
        known,
        known,
        known,
        known,
        MemoryMapContext((), "toolkit-discovery"),
        known,
    )


def _snapshot(context: TargetContext | None = None) -> dict[str, Any]:
    snapshot: dict[str, Any] = json.loads(json.dumps(asdict(context or _context())))
    return snapshot


def _plan(*calls: ToolCall) -> ActionPlan:
    if not calls:
        calls = (ToolCall("read", "memory_read", {"address": 0x20000000, "length": 4}),)
    return ActionPlan(
        "plan-1",
        "ctx-1",
        "Inspect target",
        tuple(PlannedAction(call, "Audit", (ActionEffect.OBSERVE,)) for call in calls),
    )


def _mi_result(**extra: Any) -> dict[str, Any]:
    return {"class": "done", "record": "done", "output": ["observed"], **extra}


class RecordingPolicy:
    """Explicit test-only policy; never used by production defaults."""

    def __init__(self, outcome: PolicyOutcome = PolicyOutcome.ALLOW) -> None:
        self.outcome = outcome
        self.plans: list[ActionPlan] = []

    def evaluate(self, plan: ActionPlan, context: TargetContext) -> PolicyDecision:
        self.plans.append(plan)
        return PolicyDecision(self.outcome, ("test policy",))


class RecordingRequester:
    """Inject transport failures and cancellation without a live target."""

    def __init__(self, failure: Exception | None = None, response: object = None) -> None:
        self.failure = failure
        self.response = _mi_result() if response is None else response
        self.requests: list[tuple[str, dict[str, Any], float]] = []
        self.started = asyncio.Event()
        self.block_on: str | None = None

    async def request(
        self, method: str, params: Mapping[str, Any] | None = None, timeout: float = 60.0
    ) -> Any:
        self.requests.append((method, dict(params or {}), timeout))
        if method == self.block_on:
            self.started.set()
            await asyncio.Event().wait()
        if method == "target.context":
            return _snapshot()
        if self.failure is not None:
            raise self.failure
        return self.response


def test_allow_routes_through_real_rpc_and_replaces_model_effect_claims() -> None:
    """Whole-plan policy sees local effects and exact normalized arguments before dispatch."""
    policy = RecordingPolicy()

    async def exercise() -> None:
        async with FakeRpcServer(
            {
                "target.context": _snapshot(),
                "mi.execute": _mi_result(),
                "command.execute": _mi_result(),
                "svd.peripherals": {"loaded": False, "peripherals": []},
            }
        ) as server:
            client = JsonRpcClient(server.url)
            await client.connect()
            try:
                results = await ToolGateway(client, policy).execute(
                    _plan(
                        ToolCall("read", "memory_read", {"address": 0, "length": 16, "timeout": 2}),
                        ToolCall("cmd", "ocd_command", {"command": "reset halt"}),
                        ToolCall("svd", "svd_peripherals", {}),
                    ),
                    _context(),
                )
                assert [result.call_id for result in results] == ["read", "cmd", "svd"]
                assert not any(result.is_error for result in results)
                assert json.loads(results[0].content) == {
                    "result": _mi_result(),
                    "truncated": False,
                }
                assert server.requests == [
                    ("target.context", {"timeout": 30.0}),
                    ("mi.execute", {"command": "-data-read-memory-bytes 0x0 16", "timeout": 2}),
                    ("target.context", {"timeout": 30.0}),
                    ("command.execute", {"command": "monitor reset halt", "timeout": 30}),
                    ("target.context", {"timeout": 30.0}),
                    ("svd.peripherals", {"timeout": 30}),
                ]
            finally:
                await client.close()

    asyncio.run(exercise())
    assert len(policy.plans) == 4
    assert all(plan == policy.plans[0] for plan in policy.plans)
    assert [step.effects for step in policy.plans[0].steps] == [
        (ActionEffect.READ_MEMORY,),
        (ActionEffect.UNKNOWN,),
        (ActionEffect.OBSERVE,),
    ]
    assert policy.plans[0].steps[1].call.arguments == {"command": "reset halt", "timeout": 30}


@pytest.mark.parametrize("outcome", [None, PolicyOutcome.DENY, PolicyOutcome.REQUIRE_APPROVAL])
def test_missing_policy_denial_and_required_approval_block_all_rpc(
    outcome: PolicyOutcome | None,
) -> None:
    """No permissive fallback or approved=True flag can bypass local policy."""
    requester = RecordingRequester()
    policy = None if outcome is None else RecordingPolicy(outcome)
    with pytest.raises(GatewayError, match="disabled|blocked"):
        asyncio.run(ToolGateway(requester, policy).execute(_plan(), _context()))
    assert requester.requests == []


def test_entire_plan_is_validated_before_policy_or_transport() -> None:
    """A later invalid proposal prevents even earlier valid actions from running."""
    requester = RecordingRequester()
    policy = RecordingPolicy()
    plan = _plan(
        ToolCall("ok", "target_core", {}),
        ToolCall("bad", "memory_read", {"address": 0, "length": 4097}),
    )
    with pytest.raises(ToolValidationError):
        asyncio.run(ToolGateway(requester, policy).execute(plan, _context()))
    assert requester.requests == []
    assert policy.plans == []
    with pytest.raises(GatewayError, match="supplied target"):
        asyncio.run(
            ToolGateway(requester, policy).execute(
                replace(_plan(), target_context_id="other"), _context()
            )
        )
    assert requester.requests == []


@pytest.mark.parametrize("change", ["id", "facts"])
def test_live_context_mismatch_blocks_dispatch(change: str) -> None:
    """Matching IDs alone are insufficient when the supplied facts differ."""
    live = (
        replace(_context(), context_id="other")
        if change == "id"
        else replace(_context(), mcu=ContextFact("another MCU", "configuration"))
    )

    async def exercise() -> None:
        async with FakeRpcServer({"target.context": _snapshot(live)}) as server:
            client = JsonRpcClient(server.url)
            await client.connect()
            try:
                with pytest.raises(GatewayError, match="context changed") as error:
                    await ToolGateway(client, RecordingPolicy()).execute(_plan(), _context())
                assert error.value.results == ()
                assert server.requests == [("target.context", {"timeout": 30.0})]
            finally:
                await client.close()

    asyncio.run(exercise())


def test_context_change_mid_chain_preserves_results_and_stops_before_next_action() -> None:
    """A core switch or another client changing context requires a new plan."""
    count = 0

    def context_response(params: dict[str, Any]) -> dict[str, Any]:
        nonlocal count
        count += 1
        return _snapshot() if count == 1 else _snapshot(replace(_context(), context_id="new"))

    async def exercise() -> None:
        async with FakeRpcServer(
            {
                "target.context": context_response,
                "target.select_core": {"core": {"id": 1}},
            }
        ) as server:
            client = JsonRpcClient(server.url)
            await client.connect()
            try:
                with pytest.raises(GatewayError, match="context changed") as error:
                    await ToolGateway(client, RecordingPolicy()).execute(
                        _plan(
                            ToolCall("select", "target_select_core", {"core": 1}),
                            ToolCall("read", "memory_read", {"address": 0, "length": 4}),
                        ),
                        _context(),
                    )
                assert [result.call_id for result in error.value.results] == ["select"]
                assert [method for method, _ in server.requests] == [
                    "target.context",
                    "target.select_core",
                    "target.context",
                ]
            finally:
                await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("response", [{}, RpcFailure(-32000, "lost connection")])
def test_context_collection_failure_never_dispatches_a_tool(response: Any) -> None:
    """A failed or malformed snapshot does not inherit the previous context."""

    async def exercise() -> None:
        async with FakeRpcServer({"target.context": response}) as server:
            client = JsonRpcClient(server.url)
            await client.connect()
            try:
                with pytest.raises(GatewayError, match="cannot verify"):
                    await ToolGateway(client, RecordingPolicy()).execute(_plan(), _context())
                assert len(server.requests) == 1
            finally:
                await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "failure",
    [
        RpcError(-32000, "GDB failed"),
        TimeoutError("deadline"),
        ConnectionError("disconnected"),
    ],
)
def test_failed_operation_is_uncertain_stops_the_chain_and_is_not_retried(
    failure: Exception,
) -> None:
    """Even a read can have side effects, so no transport failure is retried."""
    requester = RecordingRequester(failure=failure)
    results = asyncio.run(
        ToolGateway(requester, RecordingPolicy()).execute(
            _plan(
                ToolCall("first", "memory_read", {"address": 0, "length": 1}),
                ToolCall("second", "memory_read", {"address": 1, "length": 1}),
            ),
            _context(),
        )
    )
    assert len(results) == 1 and results[0].is_error
    assert results[0].call_id == "first"
    assert json.loads(results[0].content)["outcome"] == "unknown"
    assert [method for method, _, _ in requester.requests] == ["target.context", "mi.execute"]


@pytest.mark.parametrize(
    "response",
    [
        [],
        {"class": "done"},
        _mi_result(output="not an array"),
        _mi_result(record=7),
        _mi_result(output=[3]),
        _mi_result(extra=float("nan")),
        _mi_result(extra=object()),
        _mi_result(**{"class": "error", "record": 'error,msg="unreadable"'}),
    ],
)
def test_malformed_and_mi_error_results_are_not_success(response: object) -> None:
    """An object-shaped GDB failure is still a failure, not a successful observation."""
    requester = RecordingRequester(response=response)
    results = asyncio.run(ToolGateway(requester, RecordingPolicy()).execute(_plan(), _context()))
    assert len(results) == 1
    assert results[0].is_error


@pytest.mark.parametrize("text", ["x", '"\\', "\U0001f600"])
def test_output_and_error_content_are_bounded_in_encoded_bytes(text: str) -> None:
    """Truncation preserves valid JSON and the exact byte ceiling, even for escaped Unicode."""
    large = text * MAX_RESULT_BYTES
    requester = RecordingRequester(response=_mi_result(output=[large]))
    result = asyncio.run(ToolGateway(requester, RecordingPolicy()).execute(_plan(), _context()))[0]
    envelope = json.loads(result.content)
    assert not result.is_error
    assert envelope["truncated"] is True
    assert envelope["original_bytes"] > MAX_RESULT_BYTES
    assert len(result.content.encode("utf-8")) <= MAX_RESULT_BYTES
    requester = RecordingRequester(failure=RpcError(-32000, large))
    error = asyncio.run(ToolGateway(requester, RecordingPolicy()).execute(_plan(), _context()))[0]
    assert error.is_error
    assert json.loads(error.content)["truncated"] is True
    assert len(error.content.encode("utf-8")) <= MAX_RESULT_BYTES


@pytest.mark.parametrize("extra_byte", [0, 1])
def test_result_limit_is_applied_to_the_complete_json_envelope(extra_byte: int) -> None:
    """Exactly 64 KiB is retained; one additional byte triggers explicit truncation."""
    base = json.dumps({"result": _mi_result(output=[""]), "truncated": False})
    text = "x" * (MAX_RESULT_BYTES - len(base) + extra_byte)
    requester = RecordingRequester(response=_mi_result(output=[text]))
    result = asyncio.run(ToolGateway(requester, RecordingPolicy()).execute(_plan(), _context()))[0]
    envelope = json.loads(result.content)
    assert envelope["truncated"] is bool(extra_byte)
    assert len(result.content.encode("utf-8")) <= MAX_RESULT_BYTES
    if not extra_byte:
        assert len(result.content.encode("utf-8")) == MAX_RESULT_BYTES
        assert envelope["result"]["output"] == [text]


def test_mutation_after_validation_does_not_change_dispatch() -> None:
    """While awaiting the live context, original proposal data is no longer authoritative."""
    call = ToolCall("read", "memory_read", {"address": 0, "length": 4})

    class MutatingRequester(RecordingRequester):
        async def request(
            self, method: str, params: Mapping[str, Any] | None = None, timeout: float = 60.0
        ) -> Any:
            if method == "target.context":
                call.arguments["address"] = 100
            return await super().request(method, params, timeout)

    requester = MutatingRequester()
    asyncio.run(ToolGateway(requester, RecordingPolicy()).execute(_plan(call), _context()))
    assert requester.requests[1] == (
        "mi.execute",
        {"command": "-data-read-memory-bytes 0x0 4", "timeout": 30},
        35.0,
    )


@pytest.mark.parametrize("block_on", ["target.context", "mi.execute"])
def test_cancellation_releases_the_gateway_and_does_not_retry(block_on: str) -> None:
    """Cancellation remains an asyncio cancellation and identifies possibly in-flight work."""

    async def exercise() -> None:
        requester = RecordingRequester()
        requester.block_on = block_on
        gateway = ToolGateway(requester, RecordingPolicy())
        task = asyncio.create_task(gateway.execute(_plan(), _context()))
        await asyncio.wait_for(requester.started.wait(), 1)
        task.cancel()
        with pytest.raises(GatewayCancelledError) as error:
            await task
        assert error.value.results == ()
        assert error.value.pending_call_id == ("read" if block_on == "mi.execute" else None)
        requester.block_on = None
        results = await gateway.execute(_plan(), _context())
        assert len(results) == 1 and not results[0].is_error

    asyncio.run(exercise())


def test_cancellation_retains_previous_observations_and_identifies_pending_call() -> None:
    """A partially executed plan is not represented as an empty cancelled turn."""

    class AfterFirstRequester(RecordingRequester):
        async def request(
            self, method: str, params: Mapping[str, Any] | None = None, timeout: float = 60.0
        ) -> Any:
            if method == "mi.execute" and any(
                previous == "mi.execute" for previous, _, _ in self.requests
            ):
                self.block_on = "mi.execute"
            return await super().request(method, params, timeout)

    async def exercise() -> None:
        requester = AfterFirstRequester()
        gateway = ToolGateway(requester, RecordingPolicy())
        plan = _plan(
            ToolCall("first", "memory_read", {"address": 0, "length": 1}),
            ToolCall("second", "memory_read", {"address": 1, "length": 1}),
        )
        task = asyncio.create_task(gateway.execute(plan, _context()))
        await asyncio.wait_for(requester.started.wait(), 1)
        task.cancel()
        with pytest.raises(GatewayCancelledError) as error:
            await task
        assert [result.call_id for result in error.value.results] == ["first"]
        assert error.value.pending_call_id == "second"
        assert len(requester.requests) == 4

    asyncio.run(exercise())


def test_concurrent_plans_do_not_interleave_in_one_gateway() -> None:
    """The per-gateway lock serializes complete plans, not just individual RPC calls."""

    class YieldingRequester(RecordingRequester):
        async def request(
            self, method: str, params: Mapping[str, Any] | None = None, timeout: float = 60.0
        ) -> Any:
            await asyncio.sleep(0)
            return await super().request(method, params, timeout)

    async def exercise() -> None:
        requester = YieldingRequester()
        gateway = ToolGateway(requester, RecordingPolicy())
        plans = [
            _plan(
                *(
                    ToolCall(f"{prefix}-{i}", "gdb_command", {"command": f"{prefix}-{i}"})
                    for i in range(2)
                )
            )
            for prefix in ("a", "b")
        ]
        results = await asyncio.gather(*(gateway.execute(plan, _context()) for plan in plans))
        assert [len(result) for result in results] == [2, 2]
        assert [
            params["command"]
            for method, params, _ in requester.requests
            if method == "command.execute"
        ] == ["gdb a-0", "gdb a-1", "gdb b-0", "gdb b-1"]

    asyncio.run(exercise())
