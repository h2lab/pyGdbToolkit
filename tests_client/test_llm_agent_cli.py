# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""The agent CLI and its controller, over a loopback server and a scripted provider."""

import asyncio
from collections.abc import Callable
from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest
from textual.widgets import Input

from pyGdbClient.llm.approval import ApprovalRequest, ApprovalResponse
from pyGdbClient.llm.approval_screen import ApprovalScreen
from pyGdbClient.llm.confidentiality import TEST_DATA_WARNING
from pyGdbClient.llm.contracts import (
    ContextFact,
    MemoryMapContext,
    TargetContext,
    ToolCall,
    ToolDefinition,
)
from pyGdbClient.llm.controller import (
    AgentController,
    AgentControllerError,
    AgentSettings,
    OpenedSession,
)
from pyGdbClient.llm.orchestrator import (
    AgentEvent,
    AgentNotice,
    AgentPlan,
    AgentRunFinished,
    AgentText,
    AgentToolResults,
)
from pyGdbClient.llm.provider import (
    AuthenticationState,
    LlmSession,
    ProviderEvent,
    ProviderStatus,
    TextDelta,
    ToolCallProposed,
    TurnFinished,
    ToolResults,
    UserPrompt,
)
from pyGdbClient.llm.recollection import RECOLLECTION_HEADER
from pyGdbClient.llm.scripted import ScriptedSession, ScriptedTurn
from pyGdbClient.llm.store import ContextStore, EntryKind
from pyGdbClient.llm.tools import ToolCatalogue
from pyGdbClient.rpc import JsonRpcClient
from agent_harness import AgentApp, type_agent, until
from agent_harness import output as console
from rpc_fakes import FakeRpcServer


def _context(context_id: str = "ctx-1") -> TargetContext:
    fact = ContextFact("test", "configuration")
    return TargetContext(
        context_id,
        ContextFact("STM32U585", "lscpu"),
        fact,
        ContextFact("armv8-m.main", "gdb"),
        ContextFact("swd", "ocd"),
        ContextFact("openocd", "server"),
        fact,
        MemoryMapContext((), "gdb"),
        ContextFact(None, "svd", "no SVD loaded"),
    )


def _responses(context: TargetContext | None = None) -> dict[str, Any]:
    return {
        "target.context": json.loads(json.dumps(asdict(context or _context()))),
        "target.status": {"state": "stopped"},
        "mi.execute": {"class": "done", "record": "done", "output": ["observed"]},
    }


def _calls(*calls: ToolCall, text: str = "") -> ScriptedTurn:
    events: list[ProviderEvent] = [TextDelta(text)] if text else []
    events.extend(ToolCallProposed(call) for call in calls)
    return (*events, TurnFinished("tool_calls"))


def _answer(text: str) -> ScriptedTurn:
    return (TextDelta(text), TurnFinished())


_STATUS = ToolCall("s1", "target_status", {})
_READ = ToolCall("r1", "memory_read", {"address": 0x20000000, "length": 4})
_READY = ProviderStatus(True, AuthenticationState.NOT_REQUIRED, "Local scripted provider")


class TrackedSession(ScriptedSession):
    def __init__(
        self,
        session_id: str,
        context: TargetContext,
        tools: tuple[ToolDefinition, ...],
        turns: tuple[ScriptedTurn, ...],
    ) -> None:
        super().__init__(session_id, context, tools, turns)
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1
        await super().close()


class TrackedProvider:
    """Scripted provider recording sessions, status checks and closure."""

    provider_id = "scripted"

    def __init__(self, *turns: ScriptedTurn, status: ProviderStatus = _READY) -> None:
        self.turns = turns
        self.current_status = status
        self.sessions: list[TrackedSession] = []
        self.closed = 0

    async def status(self) -> ProviderStatus:
        return self.current_status

    async def open_session(
        self, session_id: str, context: TargetContext, tools: tuple[ToolDefinition, ...]
    ) -> LlmSession:
        session = TrackedSession(session_id, context, tools, self.turns)
        self.sessions.append(session)
        return session

    async def close(self) -> None:
        self.closed += 1


class Recorder:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []
        self.requests: list[ApprovalRequest] = []
        self.release = asyncio.Event()
        self.approve = True

    async def approver(self, request: ApprovalRequest) -> ApprovalResponse:
        self.requests.append(request)
        await self.release.wait()
        return request.respond(approved=self.approve)


async def _controller(
    server: FakeRpcServer,
    provider: TrackedProvider,
    recorder: Recorder,
    *,
    store: Path | None = None,
    session_ids: Callable[[], str] | None = None,
    **options: Any,
) -> AgentController:
    client = JsonRpcClient(server.url)
    await client.connect()
    settings = AgentSettings(provider, store_path=store, **options)
    if session_ids is None:
        return AgentController(
            settings, client, approver=recorder.approver, sink=recorder.events.append
        )
    return AgentController(
        settings,
        client,
        approver=recorder.approver,
        sink=recorder.events.append,
        session_ids=session_ids,
    )


def test_settings_reject_unbounded_approval_deadlines() -> None:
    for timeout in (0.0, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="approval_timeout"):
            AgentSettings(TrackedProvider(), approval_timeout=timeout)


def test_first_request_transmits_snapshot_and_catalogue_then_runs_policy_allowed_tools() -> None:
    async def exercise() -> None:
        provider = TrackedProvider(
            _calls(_STATUS, text="Checking\n"), _answer("Halted."), _answer("Again.")
        )
        recorder = Recorder()
        async with FakeRpcServer(_responses()) as server:
            controller = await _controller(server, provider, recorder)
            opened: list[OpenedSession] = []
            outcome = await controller.submit("State?", on_session=opened.append)
            assert outcome.reason == "completed"
            assert outcome.tool_calls == 1
            (session,) = provider.sessions
            assert opened == [OpenedSession(session.session_id, _context())]
            assert controller.session == opened[0]
            assert session.context == _context()
            assert session.tools == ToolCatalogue().definitions()
            assert server.methods[0] == "target.context"
            assert "target.status" in server.methods
            assert recorder.requests == []
            assert [type(event) for event in recorder.events].count(AgentPlan) == 1
            assert isinstance(recorder.events[-1], AgentRunFinished)
            text = "".join(e.text for e in recorder.events if isinstance(e, AgentText))
            assert text == "Checking\nHalted."

            await controller.submit("Again?", on_session=opened.append)
            assert len(opened) == 1
            assert len(provider.sessions) == 1
            await controller.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "status",
    [
        ProviderStatus(False, AuthenticationState.UNAVAILABLE, "runtime missing"),
        ProviderStatus(True, AuthenticationState.REQUIRED, "Run 'copilot login'."),
        ProviderStatus(True, AuthenticationState.EXPIRED, "Sign in again."),
    ],
)
def test_unready_provider_is_reported_before_any_target_access(status: ProviderStatus) -> None:
    async def exercise() -> None:
        provider = TrackedProvider(_answer("never"), status=status)
        async with FakeRpcServer(_responses()) as server:
            controller = await _controller(server, provider, Recorder())
            with pytest.raises(AgentControllerError, match=status.message):
                await controller.submit("hello")
            assert server.requests == []
            assert provider.sessions == []
            assert await controller.status() == status
            await controller.close()

    asyncio.run(exercise())


def test_store_retains_the_session_snapshot_and_history(tmp_path: Path) -> None:
    async def exercise() -> None:
        path = tmp_path / "agent.sqlite3"
        provider = TrackedProvider(_answer("Hi."))
        async with FakeRpcServer(_responses()) as server:
            controller = await _controller(server, provider, Recorder(), store=path)
            await controller.submit("hello")
            session_id = provider.sessions[0].session_id
            await controller.close()
        with ContextStore(path) as store:
            stored = store.get_session(session_id)
            assert stored.provider_id == "scripted"
            assert store.get_context(session_id) == _context()
            kinds = [entry.kind for entry in store.history(session_id)]
            assert kinds[0] == EntryKind.USER

    asyncio.run(exercise())


def test_new_session_closes_the_previous_one_and_refreshes_the_snapshot() -> None:
    async def exercise() -> None:
        provider = TrackedProvider(_answer("one"))
        responses = _responses()
        async with FakeRpcServer(responses) as server:
            controller = await _controller(server, provider, Recorder())
            await controller.submit("first")
            server.responses["target.context"] = _responses(_context("ctx-2"))["target.context"]
            opened = await controller.new_session()
            assert opened.context.context_id == "ctx-2"
            assert provider.sessions[0].closed == 1
            assert provider.sessions[1].context.context_id == "ctx-2"
            assert opened.session_id != provider.sessions[0].session_id
            await controller.close()

    asyncio.run(exercise())


def test_failed_open_closes_the_provider_session(tmp_path: Path) -> None:
    async def exercise() -> None:
        provider = TrackedProvider(_answer("one"))
        async with FakeRpcServer(_responses()) as server:
            controller = await _controller(
                server, provider, Recorder(), store=tmp_path / "a.sqlite3", session_ids=lambda: "s"
            )
            await controller.new_session()
            with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
                await controller.new_session()
            assert [session.closed for session in provider.sessions] == [1, 1]
            assert controller.session is None
            await controller.close()

    asyncio.run(exercise())


def test_end_and_close_release_sessions_and_provider_idempotently() -> None:
    async def exercise() -> None:
        provider = TrackedProvider(_answer("one"))
        async with FakeRpcServer(_responses()) as server:
            controller = await _controller(server, provider, Recorder())
            assert await controller.end_session() is False
            await controller.new_session()
            assert await controller.end_session() is True
            assert provider.sessions[0].closed == 1
            await controller.close()
            await controller.close()
            assert provider.closed == 1
            with pytest.raises(AgentControllerError, match="closed"):
                await controller.submit("late")

    asyncio.run(exercise())


def test_cancel_interrupts_a_request_waiting_for_the_auditor() -> None:
    async def exercise() -> None:
        provider = TrackedProvider(_calls(_READ))
        recorder = Recorder()
        async with FakeRpcServer(_responses()) as server:
            controller = await _controller(server, provider, recorder)
            assert await controller.cancel() is False
            task = asyncio.create_task(controller.submit("Read RAM"))
            while not recorder.requests:
                await asyncio.sleep(0.01)
            assert controller.running
            with pytest.raises(AgentControllerError, match="already running"):
                await controller.submit("again")
            assert await controller.cancel() is True
            outcome = await asyncio.wait_for(task, 2)
            assert outcome.reason == "cancelled"
            assert "mi.execute" not in server.methods
            await controller.close()

    asyncio.run(exercise())


def _secret_context(context_id: str = "ctx-1") -> TargetContext:
    context = _context(context_id)
    return replace(context, mcu=ContextFact("STM32U585", "lscpu password=board-pin"))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"context_limit_chars": 0}, "context_limit_chars must be a positive"),
        ({"context_limit_chars": True}, "context_limit_chars must be a positive"),
        ({"test_data": "true"}, "test_data must be a boolean"),
        ({"recollection_chars": 999}, "at least 1000"),
        ({"context_limit_chars": 3000, "recollection_chars": 2000}, "at least twice"),
    ],
)
def test_settings_validate_context_limits(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        AgentSettings(TrackedProvider(), **change)


def test_provider_receives_a_masked_snapshot_while_the_store_keeps_it(tmp_path: Path) -> None:
    async def exercise() -> None:
        path = tmp_path / "agent.sqlite3"
        provider = TrackedProvider(_answer("ok"))
        async with FakeRpcServer(_responses(_secret_context())) as server:
            controller = await _controller(server, provider, Recorder(), store=path)
            opened = await controller.new_session()
            assert opened.redactions == 1
            assert opened.context == _secret_context()
            (session,) = provider.sessions
            assert "board-pin" not in json.dumps(asdict(session.context))
            assert session.context.mcu.source == "lscpu [REDACTED:credential-assignment]"
            usage = controller.usage
            assert usage is not None and usage.used == len(json.dumps(asdict(session.context)))
            await controller.close()
        assert path.stat().st_mode & 0o777 == 0o600
        with ContextStore(path) as store:
            assert store.get_context(opened.session_id) == _secret_context()

    asyncio.run(exercise())


def test_existing_store_files_keep_their_permissions(tmp_path: Path) -> None:
    async def exercise() -> None:
        path = tmp_path / "agent.sqlite3"
        ContextStore(path).close()
        path.chmod(0o640)
        async with FakeRpcServer(_responses()) as server:
            controller = await _controller(server, TrackedProvider(), Recorder(), store=path)
            await controller.new_session()
            await controller.close()
        assert path.stat().st_mode & 0o777 == 0o640

    asyncio.run(exercise())


@pytest.mark.parametrize("approve", [True, False])
def test_test_data_preserves_analysis_without_bypassing_approval(approve: bool) -> None:
    async def exercise() -> None:
        pem = "-----BEGIN PRIVATE KEY-----\nTEST-ONLY-KEY\n-----END PRIVATE KEY-----"
        observation = (
            "0x20000000: 00 01 ab ff; pc=0x08000134 <crypto_driver+4>\n"
            "AES.CR=0x1; secret=test-vector\n" + pem
        )
        responses = _responses(_secret_context())
        responses["mi.execute"]["output"] = [observation]
        provider = TrackedProvider(_calls(_READ), _answer("analysed"))
        recorder = Recorder()
        recorder.approve = approve
        recorder.release.set()
        async with FakeRpcServer(responses) as server:
            controller = await _controller(server, provider, recorder, test_data=True)
            prompt = f"Analyse secret=test-vector with test key:\n{pem}"
            await controller.submit(prompt)
            session = provider.sessions[0]
            assert session.context == _secret_context()
            assert session.inputs[0] == UserPrompt(prompt)
            assert len(recorder.requests) == 1
            results = session.inputs[1]
            assert isinstance(results, ToolResults)
            observed = [event for event in recorder.events if isinstance(event, AgentToolResults)][
                0
            ]
            assert results.results == observed.results
            assert results.results[0].is_error is not approve
            assert ("mi.execute" in server.methods) is approve
            if approve:
                document = json.loads(results.results[0].content)
                assert document["result"]["output"] == [observation]
                assert document["truncated"] is False
            notices = [event.message for event in recorder.events if isinstance(event, AgentNotice)]
            assert TEST_DATA_WARNING in notices
            assert not any("Redacted" in message for message in notices)
            await controller.compact()
            await controller.submit("Continue")
            continued = provider.sessions[1].inputs[0]
            assert isinstance(continued, UserPrompt)
            assert prompt in continued.text
            assert controller.session is not None and controller.session.test_data
            await controller.close()

    asyncio.run(exercise())


def test_reaching_the_context_limit_continues_with_a_recollection() -> None:
    async def exercise() -> None:
        provider = TrackedProvider(_answer("x" * 2400))
        recorder = Recorder()
        ids = iter(["first", "second"])
        async with FakeRpcServer(_responses()) as server:
            controller = await _controller(
                server,
                provider,
                recorder,
                session_ids=lambda: next(ids),
                context_limit_chars=3000,
                recollection_chars=1500,
            )
            opened: list[OpenedSession] = []
            await controller.submit("measure the clock", on_session=opened.append)
            usage = controller.usage
            assert usage is not None and usage.used >= usage.limit == 3000
            await controller.submit("and now?", on_session=opened.append)
            assert [session.session_id for session in opened] == ["first", "second"]
            assert opened[1].continues == "first"
            assert 0 < opened[1].recollection_chars <= 1500
            notices = [e.message for e in recorder.events if isinstance(e, AgentNotice)]
            assert any("continuing in a new session" in notice for notice in notices)
            assert provider.sessions[0].closed == 1
            (prompt,) = provider.sessions[1].inputs
            assert isinstance(prompt, UserPrompt)
            assert prompt.text.startswith(f"{RECOLLECTION_HEADER}\nSession: first\n")
            assert "Auditor: measure the clock" in prompt.text
            assert prompt.text.endswith("Current auditor request:\nand now?")
            stored = controller.sessions()
            assert [(s.session_id, s.title) for s in stored] == [
                ("first", ""),
                ("second", "continues first"),
            ]
            await controller.close()

    asyncio.run(exercise())


def test_compact_resume_and_sessions() -> None:
    async def exercise() -> None:
        provider = TrackedProvider(_answer("noted"))
        ids = iter(["a", "b", "c", "d"])
        async with FakeRpcServer(_responses()) as server:
            controller = await _controller(
                server, provider, Recorder(), session_ids=lambda: next(ids)
            )
            assert controller.sessions() == ()
            with pytest.raises(AgentControllerError, match="no agent session"):
                await controller.compact()
            with pytest.raises(AgentControllerError, match="unknown stored session: z"):
                await controller.resume("z")
            await controller.submit("remember 42")
            compacted = await controller.compact()
            assert (compacted.session_id, compacted.continues) == ("b", "a")
            assert compacted.recollection_chars > 0
            assert controller.sessions(limit=1)[0].session_id == "b"
            resumed = await controller.resume("a")
            assert (resumed.session_id, resumed.continues) == ("c", "a")
            await controller.submit("what was it?")
            prompt = provider.sessions[2].inputs[0]
            assert isinstance(prompt, UserPrompt) and "Auditor: remember 42" in prompt.text
            await controller.end_session()
            empty = await controller.resume("b")
            assert empty.recollection_chars == 0
            assert controller.usage is not None
            await controller.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("size", [(110, 45), (80, 24)])
def test_agent_cli_runs_an_approved_chain_in_the_central_console(size: tuple[int, int]) -> None:
    async def exercise() -> None:
        provider = TrackedProvider(
            _calls(_READ, text="Reading SRAM\n"), _answer("Value \x1b[31mobserved.")
        )
        async with FakeRpcServer(_responses()) as server:
            app = AgentApp(server.url, AgentSettings(provider))
            async with app.run_test(size=size) as pilot:
                await until(pilot, lambda: app._connected)
                await type_agent(pilot, app, "Dump the first word of SRAM")
                await until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
                assert "mi.execute" not in server.methods
                await pilot.click("#approval-accept")
                await until(pilot, lambda: "Agent request completed" in console(app))
                output = console(app)
                for expected in (
                    "agent› Dump the first word of SRAM",
                    "Agent session",
                    "MCU: STM32U585 (lscpu)",
                    "Debug transport: swd (ocd)",
                    "SVD: unavailable: no SVD loaded (svd)",
                    "Reading SRAM",
                    "Agent plan",
                    "memory_read",
                    "[ok] r1",
                    "Value \ufffd[31mobserved.",
                ):
                    assert expected in output
                assert "mi.execute" in server.methods
                assert app.query_one("#command", Input).value == ""
            assert provider.closed == 1
            assert provider.sessions[0].closed == 1

    asyncio.run(exercise())


def test_agent_cli_commands_are_handled_locally() -> None:
    async def exercise() -> None:
        provider = TrackedProvider(
            _answer("never"),
            status=ProviderStatus(True, AuthenticationState.REQUIRED, "Run 'copilot login'."),
        )
        async with FakeRpcServer(_responses()) as server:
            app = AgentApp(server.url, AgentSettings(provider))
            async with app.run_test(size=(110, 45)) as pilot:
                await until(pilot, lambda: app._connected)
                for command in ("/help", "/STATUS", "/cancel", "/end", "/unknown", "/new"):
                    await type_agent(pilot, app, command)
                await until(pilot, lambda: "Agent error" in console(app))
                await app.workers.wait_for_complete()
                output = console(app)
                assert "AGENT COMMANDS" in output
                assert "not ready · authentication required" in output
                assert "Run 'copilot login'." in output
                assert "No agent request is running" in output
                assert "No agent session is open" in output
                assert "Unknown agent command: /unknown" in output
                assert "provider scripted is not ready" in output
                assert server.requests == []
                assert provider.sessions == []

    asyncio.run(exercise())


@pytest.mark.parametrize("connect", [True, False])
def test_agent_cli_explains_how_to_enable_the_agent(connect: bool) -> None:
    async def exercise() -> None:
        async with FakeRpcServer(_responses()) as server:
            app = AgentApp(server.url, None, connect=connect)
            async with app.run_test(size=(110, 45)) as pilot:
                await pilot.pause()
                placeholder = app.query_one("#agent-command", Input).placeholder
                assert "--llm" in placeholder
                for command in ("hello", "/status"):
                    await type_agent(pilot, app, command)
                await app.workers.wait_for_complete()
                assert console(app).count("Agent disabled") == 2
                assert server.requests == []

    asyncio.run(exercise())


def test_agent_cli_requires_a_server_connection() -> None:
    async def exercise() -> None:
        provider = TrackedProvider(_answer("never"))
        app = AgentApp("ws://127.0.0.1:1", AgentSettings(provider), connect=False)
        async with app.run_test(size=(110, 45)) as pilot:
            await type_agent(pilot, app, "hello")
            await type_agent(pilot, app, "/new")
            await app.workers.wait_for_complete()
            assert console(app).count("Not connected to pyGdbServer") == 2
            assert provider.sessions == []

    asyncio.run(exercise())


def test_dashboard_help_lists_agent_commands() -> None:
    async def exercise() -> None:
        app = AgentApp("ws://127.0.0.1:1", None, connect=False)
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause()
            await app.show_help()
            output = console(app)
            assert "AGENT CLI" in output
            assert "/cancel" in output

    asyncio.run(exercise())


def test_agent_cli_continuity_commands() -> None:
    async def exercise() -> None:
        provider = TrackedProvider(_answer("noted"))
        async with FakeRpcServer(_responses(_secret_context())) as server:
            app = AgentApp(server.url, AgentSettings(provider))
            async with app.run_test(size=(110, 45)) as pilot:
                await until(pilot, lambda: app._connected)
                await type_agent(pilot, app, "/sessions")
                await until(pilot, lambda: "No journaled agent session" in console(app))
                await type_agent(pilot, app, "remember 42")
                await until(pilot, lambda: "Agent request completed" in console(app))
                assert "Redacted before disclosure: 1 value(s)" in console(app)
                first = provider.sessions[0].session_id
                for command in ("/status", "/compact", "/sessions", "/resume", "/resume a b"):
                    await type_agent(pilot, app, command)
                    await app.workers.wait_for_complete()
                await type_agent(pilot, app, f"/RESUME {first}")
                await app.workers.wait_for_complete()
                await type_agent(pilot, app, "/resume missing")
                await app.workers.wait_for_complete()
                output = console(app)
                assert "Agent session context:" in output
                assert f"Continues {first}:" in output
                assert "Journaled agent sessions (oldest first)" in output
                assert f"continues {first}" in output
                assert output.count("Unknown agent command: /resume") == 2
                assert "unknown stored session: missing" in output
                assert len(provider.sessions) == 3

    asyncio.run(exercise())
