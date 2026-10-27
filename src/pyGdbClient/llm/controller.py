# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""User-interface independent lifecycle of one agent conversation over pyGdbServer."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
import json
import math
import os
from pathlib import Path
from uuid import uuid4

from .approval import AuditorApprover
from .confidentiality import TEST_DATA_WARNING, Redactor
from .context import RpcRequester, fetch_target_context
from .contracts import TargetContext
from .gateway import ToolGateway
from .orchestrator import (
    AgentBudget,
    AgentEvent,
    AgentNotice,
    AgentOrchestrator,
    AgentSink,
    AgentText,
    AgentToolResults,
    OrchestratorError,
    RunOutcome,
)
from .policy import ProtectionPolicy
from .provider import (
    AuthenticationState,
    LlmProvider,
    ProviderStatus,
    ProviderUsage,
    UsageReporter,
)
from .recollection import MIN_RECOLLECTION_CHARS, build_recollection
from .store import ContextStore, HistoryEntry, StoredSession
from .tools import ToolCatalogue

_READY = {AuthenticationState.AUTHENTICATED, AuthenticationState.NOT_REQUIRED}
_HISTORY_PAGE = 1000
_MAX_RECALLED_ENTRIES = 4096


class AgentControllerError(RuntimeError):
    """The agent cannot process the request; the message is safe to display."""


def _positive_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class AgentSettings:
    """Application-owned agent configuration; nothing here comes from the provider.

    ``context_limit_chars`` bounds the characters exchanged in one provider
    conversation (snapshot, prompts, assistant text and observations). Beyond
    it, the next request continues in a new conversation that receives a
    recollection of at most ``recollection_chars`` characters.

    ``test_data`` disables disclosure masking for analysis of test targets,
    including test cryptographic material. It never changes target protection.
    """

    provider: LlmProvider
    store_path: Path | None = None
    budget: AgentBudget = field(default_factory=AgentBudget)
    approval_timeout: float = 120.0
    redactor: Redactor = field(default_factory=Redactor)
    context_limit_chars: int = 120_000
    recollection_chars: int = 12_000
    test_data: bool = False

    def __post_init__(self) -> None:
        """Reject unbounded deadlines and inconsistent context limits."""
        timeout = self.approval_timeout
        if not isinstance(self.test_data, bool):
            raise ValueError("test_data must be a boolean")
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("approval_timeout must be finite and positive")
        limit = _positive_integer(self.context_limit_chars, "context_limit_chars")
        recollection = _positive_integer(self.recollection_chars, "recollection_chars")
        if recollection < MIN_RECOLLECTION_CHARS:
            raise ValueError(f"recollection_chars must be at least {MIN_RECOLLECTION_CHARS}")
        if limit < 2 * recollection:
            raise ValueError("context_limit_chars must be at least twice recollection_chars")


@dataclass(frozen=True)
class OpenedSession:
    """A conversation that received the transmitted target snapshot."""

    session_id: str
    context: TargetContext
    redactions: int = 0
    continues: str | None = None
    recollection_chars: int = 0
    test_data: bool = False


@dataclass(frozen=True)
class ContextUsage:
    """Characters exchanged in the open conversation, against the configured limit."""

    used: int
    limit: int


class AgentController:
    """Open, run, continue and close agent conversations bound to the live target.

    Each conversation receives a fresh ``target.context`` snapshot, masked by the
    configured redactor, and the tool catalogue. Proposed calls only reach the
    server through a gateway applying :class:`ProtectionPolicy` and the
    application's auditor approval callback. Every conversation is journaled,
    in memory unless a store file is configured, so it can be continued.
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
        self._recollection: str | None = None
        self._used = 0
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

    @property
    def usage(self) -> ContextUsage | None:
        """Return the open conversation's exchanged characters, if one is open."""
        if self._session is None:
            return None
        return ContextUsage(self._used, self._settings.context_limit_chars)

    async def status(self) -> ProviderStatus:
        """Report provider readiness without opening a conversation."""
        return await self._settings.provider.status()

    async def credits(self) -> ProviderUsage | None:
        """Return the provider's quota snapshot, or None when it does not report one."""
        provider = self._settings.provider
        if not isinstance(provider, UsageReporter):
            return None
        return await provider.usage()

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

    async def compact(self) -> OpenedSession:
        """Continue the open conversation in a new one seeded with its recollection."""
        async with self._lifecycle:
            session = self._session
            if session is None:
                raise AgentControllerError("no agent session is open")
            if self.running:
                raise AgentControllerError("an agent request is running; use /cancel first")
            await self._close_session()
            return await self._open(continues=session.session_id)

    async def resume(self, session_id: str) -> OpenedSession:
        """Cancel any request and continue a journaled conversation on a fresh snapshot."""
        async with self._lifecycle:
            try:
                self._open_store().get_session(session_id)
            except KeyError:
                raise AgentControllerError(f"unknown stored session: {session_id}") from None
            await self._close_session()
            return await self._open(continues=session_id)

    def sessions(self, limit: int = 20) -> tuple[StoredSession, ...]:
        """Return the most recently created journaled conversations, oldest first."""
        _positive_integer(limit, "limit")
        store = self._open_store()
        latest: deque[StoredSession] = deque(maxlen=limit)
        offset = 0
        while True:
            page = store.list_sessions(_HISTORY_PAGE, offset)
            latest.extend(page)
            if len(page) < _HISTORY_PAGE:
                return tuple(latest)
            offset += _HISTORY_PAGE

    async def end_session(self) -> bool:
        """Cancel any request and close the conversation; return whether one was open."""
        async with self._lifecycle:
            opened = self._orchestrator is not None
            await self._close_session()
            return opened

    async def submit(
        self, text: str, *, on_session: Callable[[OpenedSession], None] | None = None
    ) -> RunOutcome:
        """Run one auditor request, first opening or continuing a conversation if needed.

        Newly opened conversations are reported to ``on_session``, including the
        automatic continuation that follows reaching the context limit.
        """
        opened = await self.ensure_session()
        if self.running:
            raise AgentControllerError("an agent request is already running; use /cancel")
        limit = self._settings.context_limit_chars
        if opened is None and self._used >= limit:
            self._sink(
                AgentNotice(
                    f"Conversation reached {self._used} of {limit} characters; "
                    "continuing in a new session with a recollection"
                )
            )
            opened = await self.compact()
        if opened is not None and on_session is not None:
            on_session(opened)
        orchestrator = self._orchestrator
        if orchestrator is None:
            raise AgentControllerError("agent session was closed")
        recollection, self._recollection = self._recollection, None
        try:
            outcome = await orchestrator.submit(text, recollection=recollection)
        except (OrchestratorError, ValueError) as error:
            if orchestrator is self._orchestrator and self._recollection is None:
                self._recollection = recollection
            if isinstance(error, ValueError):
                raise
            raise AgentControllerError(str(error)) from None
        if orchestrator is self._orchestrator:
            self._used += len(text) + len(recollection or "")
        return outcome

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

    def _observe(self, event: AgentEvent) -> None:
        if isinstance(event, AgentText):
            self._used += len(event.text)
        elif isinstance(event, AgentToolResults):
            self._used += sum(len(result.content) for result in event.results)
        self._sink(event)

    async def _open(self, continues: str | None = None) -> OpenedSession:
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
        recollection = None
        if continues is not None:
            recollection = build_recollection(
                continues, self._history(store, continues), self._settings.recollection_chars
            )
        redactor = None if self._settings.test_data else self._settings.redactor
        if redactor is None:
            self._sink(AgentNotice(TEST_DATA_WARNING))
            disclosed, redactions = context, 0
        else:
            disclosed, redactions = redactor.context(context)
        session_id = self._session_ids()
        session = await provider.open_session(session_id, disclosed, ToolCatalogue().definitions())
        try:
            title = f"continues {continues}" if continues is not None else ""
            store.create_session(session_id, provider.provider_id, context, title)
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
                sink=self._observe,
                budget=self._settings.budget,
                store=store,
                context_client=self._client,
                redactor=redactor,
            )
        except BaseException:
            await session.close()
            raise
        self._recollection = recollection
        self._used = len(json.dumps(asdict(disclosed)))
        self._session = OpenedSession(
            session_id,
            context,
            redactions,
            continues,
            len(recollection or ""),
            self._settings.test_data,
        )
        return self._session

    async def _close_session(self) -> None:
        orchestrator, self._orchestrator, self._session = self._orchestrator, None, None
        self._recollection = None
        self._used = 0
        if orchestrator is not None:
            await orchestrator.close()

    def _open_store(self) -> ContextStore:
        if self._store is None:
            path = self._settings.store_path
            if path is None:
                self._store = ContextStore(":memory:")
            else:
                try:
                    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
                except FileExistsError:
                    pass
                self._store = ContextStore(path)
        return self._store

    @staticmethod
    def _history(store: ContextStore, session_id: str) -> tuple[HistoryEntry, ...]:
        entries: deque[HistoryEntry] = deque(maxlen=_MAX_RECALLED_ENTRIES)
        after = 0
        while True:
            page = store.history(session_id, after, _HISTORY_PAGE)
            entries.extend(page)
            if len(page) < _HISTORY_PAGE:
                return tuple(entries)
            after = page[-1].sequence
