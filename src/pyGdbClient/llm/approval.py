# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Immutable, one-attempt auditor requests; never restored from conversation history."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
import hashlib
import json
from uuid import uuid4

from .contracts import ActionPlan, PlannedAction, PolicyDecision, TargetContext
from .tools import ToolCatalogue


@dataclass(frozen=True)
class ApprovalResponse:
    """Auditor response bound to one request and its exact canonical payload."""

    request_id: str
    digest: str
    approved: bool


@dataclass(frozen=True)
class ApprovalRequest:
    """Detached display data containing plan, target, effects, risks and RPC arguments."""

    request_id: str
    digest: str
    payload_json: str

    def respond(self, *, approved: bool) -> ApprovalResponse:
        """Bind an explicit UI decision to this request, without granting replay rights."""
        return ApprovalResponse(self.request_id, self.digest, approved)


type AuditorApprover = Callable[[ApprovalRequest], Awaitable[ApprovalResponse]]


def create_approval_request(
    plan: ActionPlan, context: TargetContext, decision: PolicyDecision
) -> ApprovalRequest:
    """Freeze the normalized plan, exact RPC routes, context and policy explanation."""
    catalogue = ToolCatalogue()
    calls = tuple(catalogue.prepare(step.call) for step in plan.steps)
    normalized = ActionPlan(
        plan.plan_id,
        plan.target_context_id,
        plan.rationale,
        tuple(
            PlannedAction(call.tool_call(), step.purpose, call.effects)
            for call, step in zip(calls, plan.steps)
        ),
    )
    payload = {
        "plan": asdict(normalized),
        "context": asdict(context),
        "policy": asdict(decision),
        "rpc_calls": [
            {
                "call_id": call.call_id,
                "method": call.method,
                "params": json.loads(call.params_json),
                "timeout": call.timeout,
            }
            for call in calls
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return ApprovalRequest(
        uuid4().hex, hashlib.sha256(encoded.encode("ascii")).hexdigest(), encoded
    )
