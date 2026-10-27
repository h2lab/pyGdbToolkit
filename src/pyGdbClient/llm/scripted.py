# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Deterministic provider for exercising contracts without a network or target."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from copy import deepcopy

from .contracts import TargetContext, ToolDefinition
from .provider import (
    AuthenticationState,
    LlmSession,
    ProviderCancelledError,
    ProviderError,
    ProviderEvent,
    ProviderInput,
    ProviderStatus,
    ToolCallProposed,
    TurnFinished,
)

type ScriptedTurn = tuple[ProviderEvent, ...]


class ScriptedSession:
    """Replay fixed turns and retain inputs, without executing proposed tools."""

    def __init__(
        self,
        session_id: str,
        context: TargetContext,
        tools: tuple[ToolDefinition, ...],
        turns: tuple[ScriptedTurn, ...],
    ) -> None:
        self.session_id = session_id
        self.context = context
        self.tools = deepcopy(tools)
        self.inputs: list[ProviderInput] = []
        self._turns = deepcopy(turns)
        self._position = 0
        self._closed = False
        self._active: asyncio.Event | None = None

    async def stream(self, message: ProviderInput) -> AsyncIterator[ProviderEvent]:
        """Replay the next turn, reporting exhaustion and cancellation explicitly."""
        if self._closed:
            raise ProviderError("session is closed")
        if self._active is not None:
            raise ProviderError("a turn is already streaming")
        if self._position >= len(self._turns):
            raise ProviderError("scripted provider has no remaining turns")
        cancelled = asyncio.Event()
        self._active = cancelled
        self.inputs.append(deepcopy(message))
        turn = self._turns[self._position]
        self._position += 1
        try:
            for event in turn:
                await asyncio.sleep(0)
                if cancelled.is_set():
                    raise ProviderCancelledError("turn was cancelled")
                yield deepcopy(event)
        finally:
            self._active = None

    async def cancel(self) -> None:
        """Signal cancellation of the active turn only."""
        if self._active is not None:
            self._active.set()

    async def close(self) -> None:
        """Cancel active streaming and close the conversation idempotently."""
        self._closed = True
        await self.cancel()


class ScriptedProvider:
    """Open independent sessions replaying the same validated event script."""

    provider_id = "scripted"

    def __init__(self, turns: tuple[ScriptedTurn, ...]) -> None:
        for turn in turns:
            if not turn or not isinstance(turn[-1], TurnFinished):
                raise ValueError("each scripted turn must end with TurnFinished")
            if any(isinstance(event, TurnFinished) for event in turn[:-1]):
                raise ValueError("TurnFinished must be the last event")
            calls = [event.call for event in turn if isinstance(event, ToolCallProposed)]
            if bool(calls) != (turn[-1].reason == "tool_calls"):
                raise ValueError("tool proposals require the tool_calls finish reason")
            if len({call.call_id for call in calls}) != len(calls):
                raise ValueError("tool call IDs must be unique within a turn")
        self._turns = deepcopy(turns)

    async def status(self) -> ProviderStatus:
        """Report readiness without credentials or network access."""
        return ProviderStatus(True, AuthenticationState.NOT_REQUIRED, "Local scripted provider")

    async def open_session(
        self,
        session_id: str,
        context: TargetContext,
        tools: tuple[ToolDefinition, ...],
    ) -> LlmSession:
        """Create a fresh replay, without sharing mutable payloads between sessions."""
        if not session_id.strip():
            raise ValueError("sessions require a nonempty ID")
        return ScriptedSession(session_id, context, tools, self._turns)
