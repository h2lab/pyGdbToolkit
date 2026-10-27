# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Fail-closed, policy-gated RPC dispatch for complete agent action plans."""

from __future__ import annotations

import asyncio
import json

from websockets.exceptions import ConnectionClosed

from pyGdbClient.rpc import RpcError

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
    """Execute validated plans only when a local policy explicitly returns ALLOW.

    No policy, DENY and REQUIRE_APPROVAL all block dispatch. This layer has no
    approval bypass; auditor approval will be integrated in its own increment.
    """

    def __init__(self, client: RpcRequester, policy: TargetPolicy | None = None) -> None:
        """Bind a client-only transport and an optional application-owned policy."""
        self._client = client
        self._policy = policy
        self._catalogue = ToolCatalogue()
        self._lock = asyncio.Lock()

    async def execute(self, plan: ActionPlan, context: TargetContext) -> tuple[ToolResult, ...]:
        """Validate an entire plan, then dispatch sequentially while its context stays live.

        Failures during dispatch yield an error result and stop the chain without
        retry. Context/policy blocks raise GatewayError with earlier results.
        Cancellation propagates and never implies rollback of target operations.
        """
        calls = tuple(self._catalogue.prepare(step.call) for step in plan.steps)
        normalized = ActionPlan(
            plan.plan_id,
            plan.target_context_id,
            plan.rationale,
            tuple(
                PlannedAction(call.tool_call(), step.purpose, call.effects)
                for call, step in zip(calls, plan.steps)
            ),
        )
        if normalized.target_context_id != context.context_id:
            raise GatewayError("plan does not match the supplied target context")
        if self._policy is None:
            raise GatewayError("no target policy configured; execution is disabled")
        results: list[ToolResult] = []
        async with self._lock:
            decision = self._policy.evaluate(normalized, context)
            if decision.outcome is not PolicyOutcome.ALLOW:
                raise GatewayError(
                    f"plan blocked ({decision.outcome}): {'; '.join(decision.reasons)}",
                    decision=decision,
                )
            pending_call_id = None
            try:
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
