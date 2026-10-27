# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Asynchronous LLM provider contract and provider-independent turn events."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from .contracts import TargetContext, ToolCall, ToolDefinition, ToolResult


class AuthenticationState(StrEnum):
    """Credential state reported without exposing credential material."""

    NOT_REQUIRED = "not_required"
    REQUIRED = "required"
    AUTHENTICATED = "authenticated"
    EXPIRED = "expired"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ProviderStatus:
    """Provider readiness and sanitized diagnostic guidance."""

    available: bool
    authentication: AuthenticationState
    message: str


@dataclass(frozen=True)
class QuotaUsage:
    """Consumption of one provider quota for the current billing period."""

    name: str
    remaining_percentage: float
    used: int
    entitlement: int | None
    reset_date: str | None = None

    def __post_init__(self) -> None:
        """Reject impossible percentages and counts."""
        if not self.name.strip():
            raise ValueError("quota name must be nonempty")
        if not 0.0 <= self.remaining_percentage <= 100.0:
            raise ValueError("remaining_percentage must be between 0 and 100")
        if self.used < 0 or (self.entitlement is not None and self.entitlement < 0):
            raise ValueError("quota counts must be nonnegative")

    @property
    def unlimited(self) -> bool:
        """Return whether the entitlement has no request limit."""
        return self.entitlement is None

    @property
    def used_percentage(self) -> float:
        """Return the consumed share of the entitlement."""
        return 100.0 - self.remaining_percentage


@dataclass(frozen=True)
class ProviderUsage:
    """Quota snapshot reported by a provider, without account identity."""

    quotas: tuple[QuotaUsage, ...]


@runtime_checkable
class UsageReporter(Protocol):
    """Optional provider capability reporting remaining credits."""

    async def usage(self) -> ProviderUsage:
        """Return the current quota snapshot, or raise ProviderError."""
        ...


@dataclass(frozen=True)
class UserPrompt:
    """Auditor intent, distinct from tool observations."""

    text: str


@dataclass(frozen=True)
class ToolResults:
    """Results returned after the orchestrator has assessed proposed calls."""

    results: tuple[ToolResult, ...]


@dataclass(frozen=True)
class TextDelta:
    """One ordered piece of assistant output."""

    text: str


@dataclass(frozen=True)
class ToolCallProposed:
    """A complete tool invocation proposal, not partially streamed JSON."""

    call: ToolCall


@dataclass(frozen=True)
class TurnFinished:
    """Explicit end of a turn, optionally yielding to the local orchestrator."""

    reason: Literal["completed", "tool_calls"] = "completed"


type ProviderInput = UserPrompt | ToolResults
type ProviderEvent = TextDelta | ToolCallProposed | TurnFinished


class ProviderError(Exception):
    """Provider failure; a failed stream must not look like a completed turn."""


class ProviderCancelledError(ProviderError):
    """A turn was cancelled before it completed."""


class LlmSession(Protocol):
    """One conversation; only one turn may stream at a time."""

    @property
    def session_id(self) -> str:
        """Return the application-owned conversation identifier."""
        ...

    def stream(self, message: ProviderInput) -> AsyncIterator[ProviderEvent]:
        """Yield ordered events ending in TurnFinished, or raise ProviderError."""
        ...

    async def cancel(self) -> None:
        """Cancel the active turn, without starting or undoing target operations."""
        ...

    async def close(self) -> None:
        """Release resources idempotently and reject subsequent turns."""
        ...


class LlmProvider(Protocol):
    """Supply conversations without access to the target transport or executor."""

    @property
    def provider_id(self) -> str:
        """Return a stable provider identifier."""
        ...

    async def status(self) -> ProviderStatus:
        """Report readiness and authentication requirements without authenticating."""
        ...

    async def open_session(
        self,
        session_id: str,
        context: TargetContext,
        tools: tuple[ToolDefinition, ...],
    ) -> LlmSession:
        """Open an isolated conversation with explicit context and tool schemas."""
        ...
