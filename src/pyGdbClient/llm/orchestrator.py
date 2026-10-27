# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Bounded agent loop between an untrusted provider and the policy-gated gateway."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass
import json
import math
from typing import Any, Literal
from uuid import uuid4

from websockets.exceptions import ConnectionClosed

from pyGdbClient.rpc import RpcError

from .context import RpcRequester, TargetContextError, fetch_target_context
from .contracts import ActionPlan, JsonObject, PlannedAction, TargetContext, ToolCall, ToolResult
from .gateway import GatewayCancelledError, GatewayError, ToolGateway
from .provider import (
    LlmSession,
    ProviderCancelledError,
    ProviderError,
    ProviderInput,
    TextDelta,
    ToolCallProposed,
    ToolResults,
    TurnFinished,
    UserPrompt,
)
from .store import ContextStore, EntryKind
from .tools import ToolCatalogue, ToolValidationError

MAX_RATIONALE_CHARS = 4000

type RunReason = Literal["completed", "cancelled", "budget_exhausted", "timeout", "provider_error"]


class OrchestratorError(RuntimeError):
    """The orchestrator cannot accept this request in its current state."""


@dataclass(frozen=True)
class AgentBudget:
    """Limits applied to one auditor request, including all its tool rounds."""

    max_tool_rounds: int = 8
    max_tool_calls: int = 32
    max_calls_per_round: int = 8
    max_output_chars: int = 64_000
    max_prompt_chars: int = 16_000
    turn_timeout: float = 300.0

    def __post_init__(self) -> None:
        """Reject non-positive, non-integer or non-finite limits."""
        limits = (
            self.max_tool_rounds,
            self.max_tool_calls,
            self.max_calls_per_round,
            self.max_output_chars,
            self.max_prompt_chars,
        )
        if any(isinstance(limit, bool) or not isinstance(limit, int) for limit in limits):
            raise ValueError("budget limits must be integers")
        if any(limit < 1 for limit in limits):
            raise ValueError("budget limits must be positive")
        timeout = self.turn_timeout
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("turn_timeout must be finite and positive")


@dataclass(frozen=True)
class RunOutcome:
    """Why a request stopped, with the resources it consumed."""

    reason: RunReason
    message: str
    rounds: int
    tool_calls: int
    output_chars: int


@dataclass(frozen=True)
class AgentText:
    """Streamed assistant text, to be rendered literally."""

    text: str


@dataclass(frozen=True)
class AgentPlan:
    """A proposed chain submitted to the gateway; not an authorization."""

    plan: ActionPlan


@dataclass(frozen=True)
class AgentToolResults:
    """Observations returned to the provider, including non-executed calls."""

    results: tuple[ToolResult, ...]


@dataclass(frozen=True)
class AgentNotice:
    """Local orchestrator information for the auditor."""

    message: str
    error: bool = False


@dataclass(frozen=True)
class AgentRunFinished:
    """Final event of a request."""

    outcome: RunOutcome


type AgentEvent = AgentText | AgentPlan | AgentToolResults | AgentNotice | AgentRunFinished
type AgentSink = Callable[[AgentEvent], None]


@dataclass
class _Usage:
    rounds: int = 0
    tool_calls: int = 0
    output_chars: int = 0


@dataclass(frozen=True)
class _Turn:
    text: str
    calls: tuple[ToolCall, ...]
    reason: Literal["completed", "tool_calls"]


class _Stop(Exception):
    def __init__(self, reason: RunReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _not_executed(call_id: str, reason: str) -> ToolResult:
    content = json.dumps({"error": reason, "executed": False}, ensure_ascii=True)
    return ToolResult(call_id, content, is_error=True)


class AgentOrchestrator:
    """Run auditor requests one at a time, never executing tools outside the gateway.

    The provider only receives the session; the gateway, its transport, the
    approval callback and the store stay local. Every proposed call receives a
    result: an observation, or an explicit non-execution error the model can use.
    """

    def __init__(
        self,
        session: LlmSession,
        gateway: ToolGateway,
        context: TargetContext,
        *,
        sink: AgentSink,
        budget: AgentBudget | None = None,
        store: ContextStore | None = None,
        context_client: RpcRequester | None = None,
    ) -> None:
        """Bind one provider conversation to its gateway, target snapshot and sinks."""
        self._session = session
        self._gateway = gateway
        self._context = context
        self._sink = sink
        self._budget = budget or AgentBudget()
        self._store = store
        self._context_client = context_client
        self._catalogue = ToolCatalogue()
        self._task: asyncio.Task[Any] | None = None
        self._cancel_requested = False
        self._unanswered: tuple[str, ...] = ()
        self._requires_new_session = False
        self._closed = False
        if store is not None:
            store.save_context(session.session_id, context)

    @property
    def context(self) -> TargetContext:
        """Return the snapshot to which new plans are bound."""
        return self._context

    @property
    def running(self) -> bool:
        """Return whether a request is in progress."""
        return self._task is not None

    @property
    def requires_new_session(self) -> bool:
        """Return whether calls were left unanswered, making this conversation unusable."""
        return self._requires_new_session

    async def submit(self, text: str) -> RunOutcome:
        """Process one auditor request until completion, cancellation or a budget limit."""
        if self._closed:
            raise OrchestratorError("orchestrator is closed")
        if self._requires_new_session:
            raise OrchestratorError("conversation has unanswered tool calls; open a new session")
        if self._task is not None:
            raise OrchestratorError("a request is already running")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("prompt must be a non-empty string")
        if len(text) > self._budget.max_prompt_chars:
            raise ValueError(f"prompt exceeds {self._budget.max_prompt_chars} characters")
        task = asyncio.current_task()
        if task is None:
            raise OrchestratorError("submit must run inside an asyncio task")
        self._task = task
        self._cancel_requested = False
        usage = _Usage()
        try:
            try:
                reason, message = await self._run(text, usage)
            except _Stop as stop:
                reason, message = stop.reason, stop.message
            except asyncio.CancelledError:
                if not self._cancel_requested:
                    raise
                task.uncancel()
                reason, message = "cancelled", "request cancelled by the auditor"
            if self._unanswered:
                message += "; proposed calls were left unanswered, a new session is required"
            if reason != "completed":
                self._record(EntryKind.ERROR, {"reason": reason, "message": message})
            outcome = RunOutcome(
                reason, message, usage.rounds, usage.tool_calls, usage.output_chars
            )
            self._sink(AgentRunFinished(outcome))
            return outcome
        finally:
            if self._unanswered:
                self._requires_new_session = True
                self._unanswered = ()
            self._task = None

    async def cancel(self) -> None:
        """Interrupt the active request; started target operations are not rolled back."""
        task = self._task
        if task is None or self._cancel_requested:
            return
        self._cancel_requested = True
        task.cancel()
        await self._session.cancel()

    async def close(self) -> None:
        """Cancel any request and close the provider session idempotently."""
        if self._closed:
            return
        self._closed = True
        await self.cancel()
        await self._session.close()

    async def _run(self, text: str, usage: _Usage) -> tuple[RunReason, str]:
        self._record(EntryKind.USER, {"text": text})
        message: ProviderInput = UserPrompt(text)
        final_turn = False
        while True:
            turn = await self._stream_turn(message, usage)
            if turn.reason == "completed":
                if final_turn:
                    return "budget_exhausted", "tool budget exhausted; answered without tools"
                return "completed", "request completed"
            if final_turn:
                return "budget_exhausted", "provider kept proposing tools after budget exhaustion"
            usage.rounds += 1
            calls = turn.calls
            if (
                usage.rounds > self._budget.max_tool_rounds
                or usage.tool_calls + len(calls) > self._budget.max_tool_calls
            ):
                final_turn = True
                reason = "tool budget exhausted; conclude without further tool calls"
                results = tuple(_not_executed(call.call_id, reason) for call in calls)
                self._sink(AgentNotice("Tool budget exhausted; requesting a final answer", True))
            elif len(calls) > self._budget.max_calls_per_round:
                reason = f"too many calls in one round (max {self._budget.max_calls_per_round})"
                results = tuple(_not_executed(call.call_id, reason) for call in calls)
            else:
                usage.tool_calls += len(calls)
                results = await self._execute(turn, usage.rounds)
            self._report(results)
            message = ToolResults(results)

    async def _stream_turn(self, message: ProviderInput, usage: _Usage) -> _Turn:
        self._unanswered = ()
        parts: list[str] = []
        calls: list[ToolCall] = []
        finished: TurnFinished | None = None
        stream = self._session.stream(message)
        try:
            async with asyncio.timeout(self._budget.turn_timeout):
                async for event in stream:
                    if finished is not None:
                        raise _Stop("provider_error", "provider sent events after TurnFinished")
                    if isinstance(event, TextDelta):
                        usage.output_chars += len(event.text)
                        if usage.output_chars > self._budget.max_output_chars:
                            await self._session.cancel()
                            self._record_text(parts)
                            raise _Stop("budget_exhausted", "assistant output budget exhausted")
                        parts.append(event.text)
                        self._sink(AgentText(event.text))
                    elif isinstance(event, ToolCallProposed):
                        if any(call.call_id == event.call.call_id for call in calls):
                            raise _Stop("provider_error", "provider reused a tool call ID")
                        calls.append(event.call)
                    elif isinstance(event, TurnFinished):
                        finished = event
                    else:
                        raise _Stop("provider_error", "provider sent an unknown event")
        except TimeoutError as error:
            await self._session.cancel()
            self._record_text(parts)
            raise _Stop("timeout", "provider turn timed out") from error
        except ProviderCancelledError as error:
            self._record_text(parts)
            if self._cancel_requested:
                raise asyncio.CancelledError() from error
            raise _Stop("provider_error", f"provider cancelled the turn: {error}") from error
        except ProviderError as error:
            self._record_text(parts)
            raise _Stop("provider_error", f"provider failed: {error}") from error
        except asyncio.CancelledError:
            self._record_text(parts)
            raise
        finally:
            closer = getattr(stream, "aclose", None)
            if closer is not None:
                await closer()
        text = "".join(parts)
        self._record_text(parts)
        if finished is None:
            raise _Stop("provider_error", "provider stream ended without TurnFinished")
        if (finished.reason == "tool_calls") != bool(calls):
            raise _Stop("provider_error", "finish reason does not match proposed tool calls")
        for call in calls:
            self._record(
                EntryKind.TOOL_CALL,
                {"call_id": call.call_id, "name": call.name, "arguments": call.arguments},
            )
        self._unanswered = tuple(call.call_id for call in calls)
        return _Turn(text, tuple(calls), finished.reason)

    async def _execute(self, turn: _Turn, round_number: int) -> tuple[ToolResult, ...]:
        invalid: dict[str, str] = {}
        for call in turn.calls:
            try:
                self._catalogue.prepare(call)
            except ToolValidationError as error:
                invalid[call.call_id] = f"invalid call: {error}"
        if invalid:
            return tuple(
                _not_executed(
                    call.call_id,
                    invalid.get(call.call_id, "plan contains invalid calls; nothing executed"),
                )
                for call in turn.calls
            )
        plan = ActionPlan(
            uuid4().hex,
            self._context.context_id,
            turn.text[:MAX_RATIONALE_CHARS] or "No rationale was provided by the model.",
            tuple(PlannedAction(call, f"Agent tool round {round_number}") for call in turn.calls),
        )
        self._sink(AgentPlan(plan))
        try:
            results = await self._gateway.execute(plan, self._context)
            reason = "not executed: the chain stopped after a failed operation"
        except GatewayCancelledError as error:
            self._report(error.results)
            if error.pending_call_id is not None:
                self._sink(
                    AgentNotice(
                        f"Call {error.pending_call_id} may still complete on the target", True
                    )
                )
            raise
        except GatewayError as error:
            results = error.results
            reason = f"not executed: {error}"
            if error.decision is None:
                await self._refresh_context()
        done = {result.call_id for result in results}
        return results + tuple(
            _not_executed(call.call_id, reason) for call in turn.calls if call.call_id not in done
        )

    async def _refresh_context(self) -> None:
        if self._context_client is None:
            return
        try:
            context = await fetch_target_context(self._context_client)
        except (RpcError, OSError, ConnectionClosed, TargetContextError) as error:
            self._sink(AgentNotice(f"Cannot refresh target context: {error}", True))
            return
        if context == self._context:
            return
        if self._store is not None:
            self._store.save_context(self._session.session_id, context)
        self._context = context
        self._sink(AgentNotice(f"Target context changed; new plans use {context.context_id}"))

    def _report(self, results: tuple[ToolResult, ...]) -> None:
        if not results:
            return
        for result in results:
            self._record(EntryKind.TOOL_RESULT, asdict(result))
        self._sink(AgentToolResults(results))

    def _record_text(self, parts: list[str]) -> None:
        if parts:
            self._record(EntryKind.ASSISTANT, {"text": "".join(parts)})
            parts.clear()

    def _record(self, kind: EntryKind, payload: JsonObject) -> None:
        if self._store is not None:
            self._store.append(self._session.session_id, kind, payload, self._context.context_id)
