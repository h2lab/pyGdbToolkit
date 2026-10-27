# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""One-attempt auditor consent cannot bypass policy, context or exact-plan checks."""

import asyncio
from collections.abc import Mapping
from dataclasses import asdict, replace
import json
from typing import Any

import pytest

from pyGdbClient.llm.approval import ApprovalRequest, ApprovalResponse, create_approval_request
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
from pyGdbClient.llm.gateway import GatewayCancelledError, GatewayError, ToolGateway
from pyGdbClient.llm.policy import ProtectionPolicy
from pyGdbClient.rpc import JsonRpcClient
from rpc_fakes import FakeRpcServer


def _context() -> TargetContext:
    fact = ContextFact("test", "configuration")
    return TargetContext(
        "ctx-1", fact, fact, fact, fact, fact, fact, MemoryMapContext((), "test"), fact
    )


def _plan() -> ActionPlan:
    return ActionPlan(
        "plan-1",
        "ctx-1",
        "Inspect status register [bold red]untrusted[/]",
        (
            PlannedAction(ToolCall("meta", "target_status", {}), "Inspect state"),
            PlannedAction(
                ToolCall("read", "memory_read", {"address": 0x40000000, "length": 4}),
                "Inspect peripheral",
                (ActionEffect.OBSERVE,),
            ),
        ),
    )


def _mi_result() -> dict[str, Any]:
    return {"class": "done", "record": "done", "output": ["observed"]}


class Requester:
    """Record dispatch without importing GDB or accessing a target."""

    def __init__(self) -> None:
        self.context = _context()
        self.calls: list[str] = []
        self.dispatched = asyncio.Event()
        self.block = False

    async def request(
        self, method: str, params: Mapping[str, Any] | None = None, timeout: float = 60.0
    ) -> Any:
        self.calls.append(method)
        if method == "target.context":
            return json.loads(json.dumps(asdict(self.context)))
        self.dispatched.set()
        if self.block:
            await asyncio.Event().wait()
        return _mi_result()


def test_approval_displays_exact_chain_and_dispatches_over_rpc() -> None:
    """The UI receives normalized arguments/effects, routes, full context and local risks."""

    async def exercise() -> None:
        async def approve(request: ApprovalRequest) -> ApprovalResponse:
            data = json.loads(request.payload_json)
            assert data["plan"]["steps"][1]["effects"] == ["read_memory"]
            assert data["plan"]["steps"][1]["call"]["arguments"] == {
                "address": 0x40000000,
                "length": 4,
                "timeout": 30,
            }
            assert data["context"]["context_id"] == "ctx-1"
            assert data["policy"]["outcome"] == "require_approval"
            assert any("FIFO" in reason for reason in data["policy"]["reasons"])
            assert data["rpc_calls"][1]["params"] == {
                "command": "-data-read-memory-bytes 0x40000000 4",
                "timeout": 30,
            }
            assert len(request.digest) == 64
            assert server.requests == []
            return request.respond(approved=True)

        async with FakeRpcServer(
            {
                "target.context": json.loads(json.dumps(asdict(_context()))),
                "target.status": {"state": "stopped"},
                "mi.execute": _mi_result(),
            }
        ) as server:
            client = JsonRpcClient(server.url)
            await client.connect()
            try:
                results = await ToolGateway(client, ProtectionPolicy(), approver=approve).execute(
                    _plan(), _context()
                )
                assert len(results) == 2
                assert not any(result.is_error for result in results)
                assert [method for method, _ in server.requests] == [
                    "target.context",
                    "target.status",
                    "target.context",
                    "mi.execute",
                ]
            finally:
                await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("response", ["reject", "id", "digest"])
def test_rejection_or_mismatched_response_blocks_entire_plan(response: str) -> None:
    """Even the harmless prefix cannot run before consent for the whole chain."""
    requester = Requester()

    async def approve(request: ApprovalRequest) -> ApprovalResponse:
        result = request.respond(approved=response != "reject")
        if response == "id":
            result = replace(result, request_id="other")
        elif response == "digest":
            result = replace(result, digest="other")
        return result

    with pytest.raises(GatewayError, match="rejected|mismatched"):
        asyncio.run(
            ToolGateway(requester, ProtectionPolicy(), approver=approve).execute(
                _plan(), _context()
            )
        )
    assert requester.calls == []


@pytest.mark.parametrize("denied", [True, False])
def test_local_deny_and_metadata_allow_never_prompt(denied: bool) -> None:
    """Consent never overrides a local denial, and safe metadata needs no consent."""

    async def approve(request: ApprovalRequest) -> ApprovalResponse:
        pytest.fail("the auditor must not be asked")

    requester = Requester()
    gateway = ToolGateway(
        requester,
        ProtectionPolicy(denied_tools=("memory_read",) if denied else ()),
        approver=approve,
    )
    if denied:
        with pytest.raises(GatewayError, match="blocked"):
            asyncio.run(gateway.execute(_plan(), _context()))
        assert requester.calls == []
    else:
        plan = replace(_plan(), steps=(_plan().steps[0],))
        assert len(asyncio.run(gateway.execute(plan, _context()))) == 1


def test_repeat_execution_requires_new_consent_even_for_identical_plan() -> None:
    """A previous response is not a reusable capability, including for unchanged contents."""

    async def exercise() -> None:
        responses: list[ApprovalResponse] = []
        requests: list[ApprovalRequest] = []

        async def approve(request: ApprovalRequest) -> ApprovalResponse:
            requests.append(request)
            if not responses:
                responses.append(request.respond(approved=True))
            return responses[0]

        requester = Requester()
        gateway = ToolGateway(requester, ProtectionPolicy(), approver=approve)
        await gateway.execute(_plan(), _context())
        requester.calls.clear()
        with pytest.raises(GatewayError, match="mismatched"):
            await gateway.execute(_plan(), _context())
        assert requester.calls == []
        assert requests[0].digest == requests[1].digest
        assert requests[0].request_id != requests[1].request_id

    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["arguments", "rationale", "purpose", "context", "policy"])
def test_digest_binds_complete_snapshot(change: str) -> None:
    """Same plan/context IDs do not authorize a different command, explanation or target."""
    plan, context = _plan(), _context()
    decision = ProtectionPolicy().evaluate(plan, context)
    before = create_approval_request(plan, context, decision)
    if change == "arguments":
        plan.steps[1].call.arguments["address"] = 0
    elif change == "rationale":
        plan = replace(plan, rationale="changed")
    elif change == "purpose":
        plan = replace(plan, steps=(replace(plan.steps[0], purpose="changed"), plan.steps[1]))
    elif change == "context":
        context = replace(context, mcu=ContextFact("different MCU", "test"))
    else:
        decision = PolicyDecision(PolicyOutcome.REQUIRE_APPROVAL, ("different risk",))
    after = create_approval_request(plan, context, decision)
    assert before.digest != after.digest
    assert "different MCU" not in before.payload_json


@pytest.mark.parametrize("change", ["id", "facts"])
def test_context_changed_while_auditor_reviewed_blocks_dispatch(change: str) -> None:
    requester = Requester()

    async def approve(request: ApprovalRequest) -> ApprovalResponse:
        requester.context = (
            replace(_context(), context_id="changed")
            if change == "id"
            else replace(_context(), mcu=ContextFact("another MCU", "configuration"))
        )
        return request.respond(approved=True)

    with pytest.raises(GatewayError, match="context changed"):
        asyncio.run(
            ToolGateway(requester, ProtectionPolicy(), approver=approve).execute(
                _plan(), _context()
            )
        )
    assert requester.calls == ["target.context"]


def test_policy_is_rechecked_after_consent() -> None:
    class ChangingPolicy:
        def __init__(self) -> None:
            self.denied = False

        def evaluate(self, plan: ActionPlan, context: TargetContext) -> PolicyDecision:
            return PolicyDecision(
                PolicyOutcome.DENY if self.denied else PolicyOutcome.REQUIRE_APPROVAL,
                ("local policy",),
            )

    policy = ChangingPolicy()
    requester = Requester()

    async def approve(request: ApprovalRequest) -> ApprovalResponse:
        policy.denied = True
        return request.respond(approved=True)

    with pytest.raises(GatewayError, match="policy changed") as error:
        asyncio.run(ToolGateway(requester, policy, approver=approve).execute(_plan(), _context()))
    assert error.value.decision == PolicyDecision(PolicyOutcome.DENY, ("local policy",))
    assert requester.calls == ["target.context"]


def test_mutating_original_proposal_does_not_change_displayed_or_executed_calls() -> None:
    plan = _plan()

    async def exercise() -> None:
        async def approve(request: ApprovalRequest) -> ApprovalResponse:
            plan.steps[1].call.arguments["address"] = 0
            assert (
                json.loads(request.payload_json)["plan"]["steps"][1]["call"]["arguments"]["address"]
                == 0x40000000
            )
            return request.respond(approved=True)

        async with FakeRpcServer(
            {
                "target.context": json.loads(json.dumps(asdict(_context()))),
                "target.status": {"state": "stopped"},
                "mi.execute": _mi_result(),
            }
        ) as server:
            client = JsonRpcClient(server.url)
            await client.connect()
            try:
                await ToolGateway(client, ProtectionPolicy(), approver=approve).execute(
                    plan, _context()
                )
                assert server.requests[-1][1]["command"] == "-data-read-memory-bytes 0x40000000 4"
            finally:
                await client.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("cancel", [True, False])
def test_timeout_and_cancellation_never_dispatch_or_retain_consent(cancel: bool) -> None:
    async def exercise() -> None:
        started = asyncio.Event()
        cleaned = asyncio.Event()
        requests: list[ApprovalRequest] = []

        async def approve(request: ApprovalRequest) -> ApprovalResponse:
            requests.append(request)
            if len(requests) > 1:
                return request.respond(approved=False)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
            raise AssertionError("unreachable")

        requester = Requester()
        gateway = ToolGateway(
            requester,
            ProtectionPolicy(),
            approver=approve,
            approval_timeout=10 if cancel else 0.01,
        )
        task = asyncio.create_task(gateway.execute(_plan(), _context()))
        await started.wait()
        if cancel:
            task.cancel()
            with pytest.raises(GatewayCancelledError) as error:
                await task
            assert error.value.results == ()
            assert error.value.pending_call_id is None
        else:
            with pytest.raises(GatewayError, match="expired"):
                await task
        assert cleaned.is_set()
        assert requester.calls == []
        with pytest.raises(GatewayError, match="rejected"):
            await gateway.execute(_plan(), _context())
        assert requests[0].request_id != requests[1].request_id

    asyncio.run(exercise())


def test_cancellation_after_approval_preserves_uncertain_in_flight_action() -> None:
    async def exercise() -> None:
        async def approve(request: ApprovalRequest) -> ApprovalResponse:
            return request.respond(approved=True)

        requester = Requester()
        requester.block = True
        gateway = ToolGateway(requester, ProtectionPolicy(), approver=approve)
        task = asyncio.create_task(gateway.execute(_plan(), _context()))
        await requester.dispatched.wait()
        task.cancel()
        with pytest.raises(GatewayCancelledError) as error:
            await task
        assert error.value.pending_call_id == "meta"
        assert error.value.results == ()
        assert requester.calls == ["target.context", "target.status"]

    asyncio.run(exercise())


def test_late_response_cannot_extend_deadline_by_suppressing_cancellation() -> None:
    async def approve(request: ApprovalRequest) -> ApprovalResponse:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return request.respond(approved=True)
        raise AssertionError("unreachable")

    requester = Requester()
    gateway = ToolGateway(requester, ProtectionPolicy(), approver=approve, approval_timeout=0.01)
    with pytest.raises(GatewayError, match="expired"):
        asyncio.run(gateway.execute(_plan(), _context()))
    assert requester.calls == []


@pytest.mark.parametrize("timeout", [True, 0, -1, float("inf"), float("nan")])
def test_approval_deadline_must_be_finite_and_positive(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        ToolGateway(Requester(), ProtectionPolicy(), approval_timeout=timeout)
