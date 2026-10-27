# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Fail-closed, policy-gated RPC dispatch for complete agent action plans."""

from __future__ import annotations

import asyncio
import json
import math

from websockets.exceptions import ConnectionClosed

from pyGdbClient.rpc import RpcError

from .approval import AuditorApprover, create_approval_request
from .context import RpcRequester, TargetContextError, fetch_target_context
from .contracts import (
    ActionPlan,
    PlannedAction,
    PolicyDecision,
    PolicyOutcome,
    TargetContext,
    TargetPolicy,
    ToolResult,
)
from .tools import ToolCatalogue

MAX_RESULT_BYTES = 64 * 1024


class GatewayError(RuntimeError):
    """A plan was blocked or interrupted; completed observations remain available."""

    def __init__(
        self,
        message: str,
        results: tuple[ToolResult, ...] = (),
        *,
        decision: PolicyDecision | None = None,
    ) -> None:
        super().__init__(message)
        self.results = results
        self.decision = decision


class GatewayCancelledError(asyncio.CancelledError):
    """Cancellation preserving prior observations and the possibly in-flight call ID."""

    def __init__(self, results: tuple[ToolResult, ...], pending_call_id: str | None) -> None:
        super().__init__("plan cancelled; an in-flight target operation may still complete")
        self.results = results
        self.pending_call_id = pending_call_id


def _failure(call_id: str, message: str) -> ToolResult:
    content = json.dumps({"error": message, "outcome": "unknown"}, ensure_ascii=True)
    if len(content) > MAX_RESULT_BYTES:
        content = json.dumps(
            {
                "error": json.dumps(message, ensure_ascii=True)[: MAX_RESULT_BYTES // 4],
                "outcome": "unknown",
                "truncated": True,
            },
            ensure_ascii=True,
        )
    return ToolResult(call_id, content, is_error=True)


def _result(call_id: str, response: object, command_result: bool) -> ToolResult:
    if not isinstance(response, dict):
        return _failure(call_id, "RPC result must be an object; target outcome is unknown")
    if command_result and (
        response.get("class") not in ("done", "running", "connected", "error", "exit")
        or not isinstance(response.get("record"), str)
        or not isinstance(response.get("output"), list)
        or not all(isinstance(line, str) for line in response["output"])
    ):
        return _failure(call_id, "invalid GDB/MI result; target outcome is unknown")
    try:
        serialized = json.dumps(response, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError):
        return _failure(call_id, "RPC result is not valid JSON; target outcome is unknown")
    content = json.dumps({"result": response, "truncated": False}, ensure_ascii=True)
    if len(content) > MAX_RESULT_BYTES:
        content = json.dumps(
            {
                "preview": serialized[: MAX_RESULT_BYTES // 4],
                "original_bytes": len(serialized),
                "truncated": True,
            },
            ensure_ascii=True,
        )
    return ToolResult(call_id, content, is_error=response.get("class") == "error")


class ToolGateway:
    """Execute validated plans with local policy and optional one-attempt auditor consent.

    No policy and DENY always block dispatch. REQUIRE_APPROVAL needs an explicit
    application-owned callback; providers must never receive this callback.
    """

    def __init__(
        self,
        client: RpcRequester,
        policy: TargetPolicy | None = None,
        *,
        approver: AuditorApprover | None = None,
        approval_timeout: float = 120.0,
    ) -> None:
        """Bind transport, policy and an optional auditor UI with a finite deadline."""
        if (
            isinstance(approval_timeout, bool)
            or not math.isfinite(approval_timeout)
            or approval_timeout <= 0
        ):
            raise ValueError("approval_timeout must be finite and positive")
        self._client = client
        self._policy = policy
        self._approver = approver
        self._approval_timeout = approval_timeout
        self._catalogue = ToolCatalogue()
        self._lock = asyncio.Lock()

    async def execute(self, plan: ActionPlan, context: TargetContext) -> tuple[ToolResult, ...]:
        """Validate an entire plan, then dispatch sequentially while its context stays live.

        Failures during dispatch yield an error result and stop the chain without
        retry. Context/policy blocks raise GatewayError with earlier results.
        Cancellation propagates and never implies rollback of target operations.
        """
        calls = tuple(self._catalogue.prepare(step.call) for step in plan.steps)
        plan_id, context_id, rationale = plan.plan_id, plan.target_context_id, plan.rationale
        purposes = tuple(step.purpose for step in plan.steps)

        def normalized() -> ActionPlan:
            return ActionPlan(
                plan_id,
                context_id,
                rationale,
                tuple(
                    PlannedAction(call.tool_call(), purpose, call.effects)
                    for call, purpose in zip(calls, purposes)
                ),
            )

        if context_id != context.context_id:
            raise GatewayError("plan does not match the supplied target context")
        if self._policy is None:
            raise GatewayError("no target policy configured; execution is disabled")
        results: list[ToolResult] = []
        async with self._lock:
            decision = self._policy.evaluate(normalized(), context)
            if decision.outcome not in (PolicyOutcome.ALLOW, PolicyOutcome.REQUIRE_APPROVAL) or (
                decision.outcome is PolicyOutcome.REQUIRE_APPROVAL and self._approver is None
            ):
                raise GatewayError(
                    f"plan blocked ({decision.outcome}): {'; '.join(decision.reasons)}",
                    decision=decision,
                )
            pending_call_id = None
            try:
                if decision.outcome is PolicyOutcome.REQUIRE_APPROVAL:
                    await self._approve(normalized(), context, decision)
                for call in calls:
                    try:
                        live = await fetch_target_context(self._client)
                    except (RpcError, OSError, ConnectionClosed, TargetContextError) as error:
                        raise GatewayError(
                            f"cannot verify live target context: {error}", tuple(results)
                        ) from error
                    if live != context:
                        raise GatewayError(
                            "target context changed; replan before dispatch", tuple(results)
                        )
                    current = self._policy.evaluate(normalized(), context)
                    if current != decision:
                        raise GatewayError(
                            "target policy changed; a fresh assessment is required",
                            tuple(results),
                            decision=current,
                        )
                    pending_call_id = call.call_id
                    try:
                        response = await self._client.request(
                            call.method, json.loads(call.params_json), timeout=call.timeout + 5
                        )
                    except (RpcError, OSError, ConnectionClosed) as error:
                        results.append(
                            _failure(
                                call.call_id, f"RPC failed; target outcome is unknown: {error}"
                            )
                        )
                        break
                    result = _result(
                        call.call_id, response, call.method in ("command.execute", "mi.execute")
                    )
                    results.append(result)
                    pending_call_id = None
                    if result.is_error:
                        break
            except asyncio.CancelledError as error:
                raise GatewayCancelledError(tuple(results), pending_call_id) from error
        return tuple(results)

    async def _approve(
        self, plan: ActionPlan, context: TargetContext, decision: PolicyDecision
    ) -> None:
        if self._approver is None:
            raise GatewayError("no auditor approver configured", decision=decision)
        request = create_approval_request(plan, context, decision)
        deadline = asyncio.get_running_loop().time() + self._approval_timeout
        try:
            response = await asyncio.wait_for(
                self._approver(request), timeout=self._approval_timeout
            )
        except TimeoutError as error:
            raise GatewayError("auditor approval expired", decision=decision) from error
        if asyncio.get_running_loop().time() >= deadline:
            raise GatewayError("auditor approval expired", decision=decision)
        if (
            response.request_id != request.request_id
            or response.digest != request.digest
            or response.approved is not True
        ):
            raise GatewayError(
                "auditor rejected the plan or returned a mismatched approval", decision=decision
            )
