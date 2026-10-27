# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""User-interface independent lifecycle of one agent conversation over pyGdbServer."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import math
from pathlib import Path
from uuid import uuid4

from .approval import AuditorApprover
from .context import RpcRequester, fetch_target_context
from .contracts import TargetContext
from .gateway import ToolGateway
from .orchestrator import AgentBudget, AgentOrchestrator, AgentSink, OrchestratorError, RunOutcome
from .policy import ProtectionPolicy
from .provider import AuthenticationState, LlmProvider, ProviderStatus
from .store import ContextStore
from .tools import ToolCatalogue

_READY = {AuthenticationState.AUTHENTICATED, AuthenticationState.NOT_REQUIRED}


class AgentControllerError(RuntimeError):
    """The agent cannot process the request; the message is safe to display."""


@dataclass(frozen=True)
class AgentSettings:
    """Application-owned agent configuration; nothing here comes from the provider."""

    provider: LlmProvider
    store_path: Path | None = None
    budget: AgentBudget = field(default_factory=AgentBudget)
    approval_timeout: float = 120.0

    def __post_init__(self) -> None:
        """Reject an unbounded auditor deadline."""
        timeout = self.approval_timeout
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("approval_timeout must be finite and positive")


@dataclass(frozen=True)
class OpenedSession:
    """A conversation that received the transmitted target snapshot."""

    session_id: str
    context: TargetContext


class AgentController:
    """Open, run and close agent conversations bound to the live target context.

    Each conversation receives a fresh ``target.context`` snapshot and the tool
    catalogue. Proposed calls only reach the server through a gateway applying
    :class:`ProtectionPolicy` and the application's auditor approval callback.
    """

    def __init__(
        self,
        settings: AgentSettings,
        client: RpcRequester,
        *,
        approver: AuditorApprover,
        sink: AgentSink,
        session_ids: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        """Bind the provider to the server transport and the auditor interface."""
        self._settings = settings
        self._client = client
        self._approver = approver
        self._sink = sink
        self._session_ids = session_ids
        self._lifecycle = asyncio.Lock()
        self._orchestrator: AgentOrchestrator | None = None
        self._session: OpenedSession | None = None
        self._store: ContextStore | None = None
        self._closed = False

    @property
    def provider_id(self) -> str:
        """Return the configured provider identifier."""
        return self._settings.provider.provider_id

    @property
    def session(self) -> OpenedSession | None:
        """Return the open conversation, if any."""
        return self._session

    @property
    def running(self) -> bool:
        """Return whether an auditor request is in progress."""
        return self._orchestrator is not None and self._orchestrator.running

    async def status(self) -> ProviderStatus:
        """Report provider readiness without opening a conversation."""
        return await self._settings.provider.status()

    async def ensure_session(self) -> OpenedSession | None:
        """Open a conversation when none exists; return it only when newly opened."""
        async with self._lifecycle:
            if self._orchestrator is None:
                return await self._open()
            if self._orchestrator.requires_new_session:
                raise AgentControllerError(
                    "the conversation has unanswered tool calls; use /new to open a session"
                )
            return None

    async def new_session(self) -> OpenedSession:
        """Cancel any request, close the conversation and open one on a fresh snapshot."""
        async with self._lifecycle:
            await self._close_session()
            return await self._open()

    async def end_session(self) -> bool:
        """Cancel any request and close the conversation; return whether one was open."""
        async with self._lifecycle:
            opened = self._orchestrator is not None
            await self._close_session()
            return opened

    async def submit(
        self, text: str, *, on_session: Callable[[OpenedSession], None] | None = None
    ) -> RunOutcome:
        """Run one auditor request, first opening a conversation reported to ``on_session``."""
        opened = await self.ensure_session()
        if opened is not None and on_session is not None:
            on_session(opened)
        orchestrator = self._orchestrator
        if orchestrator is None:
            raise AgentControllerError("agent session was closed")
        if orchestrator.running:
            raise AgentControllerError("an agent request is already running; use /cancel")
        try:
            return await orchestrator.submit(text)
        except OrchestratorError as error:
            raise AgentControllerError(str(error)) from None

    async def cancel(self) -> bool:
        """Interrupt the running request; return whether one was running."""
        orchestrator = self._orchestrator
        if orchestrator is None or not orchestrator.running:
            return False
        await orchestrator.cancel()
        return True

    async def close(self) -> None:
        """Close the conversation, the provider and the store idempotently."""
        if self._closed:
            return
        self._closed = True
        async with self._lifecycle:
            try:
                await self._close_session()
                close = getattr(self._settings.provider, "close", None)
                if close is not None:
                    await close()
            finally:
                if self._store is not None:
                    self._store.close()
                    self._store = None

    async def _open(self) -> OpenedSession:
        if self._closed:
            raise AgentControllerError("agent controller is closed")
        provider = self._settings.provider
        status = await provider.status()
        if not status.available or status.authentication not in _READY:
            raise AgentControllerError(
                f"provider {provider.provider_id} is not ready "
                f"({status.authentication.value}): {status.message}"
            )
        context = await fetch_target_context(self._client)
        store = self._open_store()
        session_id = self._session_ids()
        session = await provider.open_session(session_id, context, ToolCatalogue().definitions())
        try:
            if store is not None:
                store.create_session(session_id, provider.provider_id, context)
            gateway = ToolGateway(
                self._client,
                ProtectionPolicy(),
                approver=self._approver,
                approval_timeout=self._settings.approval_timeout,
            )
            self._orchestrator = AgentOrchestrator(
                session,
                gateway,
                context,
                sink=self._sink,
                budget=self._settings.budget,
                store=store,
                context_client=self._client,
            )
        except BaseException:
            await session.close()
            raise
        self._session = OpenedSession(session_id, context)
        return self._session

    async def _close_session(self) -> None:
        orchestrator, self._orchestrator, self._session = self._orchestrator, None, None
        if orchestrator is not None:
            await orchestrator.close()

    def _open_store(self) -> ContextStore | None:
        if self._store is None and self._settings.store_path is not None:
            self._store = ContextStore(self._settings.store_path)
        return self._store
