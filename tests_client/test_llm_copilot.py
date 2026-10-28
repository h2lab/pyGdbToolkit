# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Exercise the pinned SDK over an offline transport, without tokens or a target."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from dataclasses import asdict, replace
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError
import json
from pathlib import Path
import sys
from typing import Any
from uuid import uuid4

from copilot import CopilotClient
from copilot.generated.rpc import ServerRpc
from copilot.session_events import SessionEvent
import pytest

from pyGdbClient.llm import copilot as adapter
from pyGdbClient.llm.approval import ApprovalRequest, ApprovalResponse
from pyGdbClient.llm.contracts import ToolCall, ToolDefinition, ToolResult
from pyGdbClient.llm.copilot import CopilotConfig, CopilotProvider, RUNTIME_VERSION
from pyGdbClient.llm.gateway import ToolGateway
from pyGdbClient.llm.orchestrator import AgentOrchestrator
from pyGdbClient.llm.policy import ProtectionPolicy
from pyGdbClient.llm.store import ContextStore
from pyGdbClient.llm.provider import (
    AuthenticationState,
    ProviderCancelledError,
    ProviderError,
    ProviderUsage,
    QuotaUsage,
    TextDelta,
    ToolCallProposed,
    ToolResults,
    TurnFinished,
    UsageReporter,
    UserPrompt,
)
from test_llm_contracts import _context
from pyGdbClient.rpc import JsonRpcClient
from rpc_fakes import FakeRpcServer

_SECRET = "do-not-disclose-token-or-provider-response"
_TOOLS = (
    ToolDefinition("target_status", "Read known state", {"type": "object"}),
    ToolDefinition("gdb_command", "Propose GDB command", {"type": "object"}),
)


def _quota(
    *, remaining: float, used: int, entitlement: int, unlimited: bool = False
) -> dict[str, Any]:
    return {
        "entitlementRequests": entitlement,
        "isUnlimitedEntitlement": unlimited,
        "overage": 0.0,
        "overageAllowedWithExhaustedQuota": False,
        "remainingPercentage": remaining,
        "usageAllowedWithExhaustedQuota": False,
        "usedRequests": used,
        "resetDate": "2026-11-01T00:00:00Z",
    }


class Runtime:
    """Keep actual SDK clients, sessions, events and generated RPC serialization."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.path = Path(sys.executable).resolve()
        self.config = CopilotConfig(self.path, rpc_timeout=0.1)
        self.clients: list[CopilotClient] = []
        self.options: list[dict[str, Any]] = []
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.turns: list[list[tuple[str, dict[str, Any]]]] = []
        self.authenticated = True
        self.runtime_version = RUNTIME_VERSION
        self.feedback_success = True
        self.isolation_success = True
        self.sequential = False
        self.delayed: list[tuple[str, dict[str, Any]]] = []
        self.fail: str | None = None
        self.quotas: dict[str, Any] = {
            "premium_interactions": _quota(remaining=37.5, used=1875, entitlement=3000),
            "chat": _quota(remaining=100.0, used=0, entitlement=-1, unlimited=True),
        }
        self.started = 0
        self.stopped = 0
        self.sent = asyncio.Event()
        self.directory: str | None = None
        original = CopilotClient

        def client(**kwargs: Any) -> CopilotClient:
            result = original(**kwargs)
            self.clients.append(result)
            self.options.append(kwargs)
            self.directory = kwargs["working_directory"]
            monkeypatch.setattr(result, "_client", self)
            monkeypatch.setattr(result, "start", self.start)
            monkeypatch.setattr(result, "stop", self.stop)
            return result

        monkeypatch.setattr("copilot.CopilotClient", client)

    async def start(self) -> None:
        self.started += 1
        if self.fail == "start":
            raise OSError(_SECRET)
        self.clients[-1]._rpc = ServerRpc(self)  # type: ignore[arg-type]

    async def stop(self) -> None:
        self.stopped += 1
        if self.fail == "stop":
            raise OSError(_SECRET)

    def emit(self, sid: str, kind: str, data: dict[str, Any], **extra: Any) -> None:
        session = self.clients[-1]._get_session(sid)
        assert session is not None
        event = SessionEvent.from_dict(
            {
                "id": str(uuid4()),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "type": kind,
                "data": {"sessionId": sid, **data},
                **extra,
            }
        )
        session._dispatch_event(event)

    def next_turn(self, sid: str) -> None:
        if self.turns:
            requested = False
            for kind, data in self.turns.pop(0):
                if self.sequential and kind == "external_tool.requested":
                    if requested:
                        self.delayed.append((kind, data))
                        continue
                    requested = True
                self.emit(sid, kind, data)

    async def request(self, method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == self.fail:
            raise OSError(_SECRET)
        if method == "status.get":
            return {"version": self.runtime_version, "protocolVersion": 3}
        if method == "auth.getStatus":
            return {
                "isAuthenticated": self.authenticated,
                "statusMessage": _SECRET,
                "login": _SECRET,
            }
        if method == "session.create":
            return {"sessionId": params["sessionId"]}
        if method == "session.options.update":
            return {"success": self.isolation_success}
        if method == "session.send":
            self.sent.set()
            self.next_turn(params["sessionId"])
            return {"messageId": str(uuid4())}
        if method == "session.tools.handlePendingToolCall":
            pending = params["requestId"]
            if self.delayed:
                kind, data = self.delayed.pop(0)
                self.emit(params["sessionId"], kind, data)
            # The next model response starts only once the entire batch has results.
            if pending == "request-2" or pending == "request-1" and self.batch_size == 1:
                self.next_turn(params["sessionId"])
            return {"success": self.feedback_success}
        if method == "session.abort":
            self.emit(params["sessionId"], "session.idle", {})
            return {}
        if method == "session.detach":
            return {"success": True}
        if method == "session.permissions.handlePendingPermissionRequest":
            return {"success": True}
        if method == "account.getQuota":
            return {"quotaSnapshots": self.quotas}
        raise AssertionError(f"Unscripted SDK method {method}")

    @property
    def batch_size(self) -> int:
        return getattr(self, "_batch_size", 1)

    def tools(self, count: int = 1, arguments: object = None) -> list[tuple[str, dict[str, Any]]]:
        self._batch_size = count
        calls = [
            {"toolCallId": f"call-{i}", "name": f"pygdb_{i-1}", "arguments": arguments or {}}
            for i in range(1, count + 1)
        ]
        return [
            (
                "assistant.message",
                {"messageId": "msg", "content": "Inspect safely", "toolRequests": calls},
            ),
            *[
                (
                    "external_tool.requested",
                    {
                        "requestId": f"request-{i}",
                        "toolCallId": call["toolCallId"],
                        "toolName": call["name"],
                        "arguments": call["arguments"],
                    },
                )
                for i, call in enumerate(calls, 1)
            ],
        ]

    def params(self, method: str) -> list[dict[str, Any]]:
        return [params for name, params in self.requests if name == method]


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Runtime:
    return Runtime(monkeypatch, tmp_path)


def test_quota_snapshot_reports_percentages_without_account_identity(runtime: Runtime) -> None:
    async def exercise() -> None:
        provider = CopilotProvider(runtime.config)
        usage = await provider.usage()
        assert isinstance(provider, UsageReporter)
        assert usage == ProviderUsage(
            (
                QuotaUsage("chat", 100.0, 0, None, "2026-11-01T00:00:00Z"),
                QuotaUsage("premium_interactions", 37.5, 1875, 3000, "2026-11-01T00:00:00Z"),
            )
        )
        assert usage.quotas[1].used_percentage == 62.5
        assert runtime.params("account.getQuota") == [{}]
        await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "quotas",
    [
        {"premium": {"remainingPercentage": 50}},
        {"": _quota(remaining=50.0, used=1, entitlement=10)},
    ],
)
def test_invalid_quota_snapshot_is_an_explicit_provider_error(
    runtime: Runtime, quotas: dict[str, Any]
) -> None:
    async def exercise() -> None:
        runtime.quotas = quotas
        provider = CopilotProvider(runtime.config)
        with pytest.raises(ProviderError, match="quota"):
            await provider.usage()
        await provider.close()

    asyncio.run(exercise())


def test_quota_failure_never_leaks_vendor_details(runtime: Runtime) -> None:
    async def exercise() -> None:
        runtime.fail = "account.getQuota"
        provider = CopilotProvider(runtime.config)
        with pytest.raises(ProviderError) as error:
            await provider.usage()
        assert _SECRET not in str(error.value)
        await provider.close()

    asyncio.run(exercise())


def _answer() -> list[tuple[str, dict[str, Any]]]:
    return [
        ("assistant.message_delta", {"messageId": "answer", "deltaContent": "Stopped"}),
        ("assistant.message", {"messageId": "answer", "content": "Stopped"}),
        ("assistant.turn_end", {"turnId": "turn"}),
        ("session.idle", {}),
    ]


def test_readiness_and_explicit_token_are_sanitized(runtime: Runtime) -> None:
    async def exercise() -> None:
        provider = CopilotProvider(replace(runtime.config, github_token=_SECRET))
        assert _SECRET not in repr(provider._config)
        status = await provider.status()
        assert status.authentication == AuthenticationState.AUTHENTICATED, status
        assert _SECRET not in repr(status)
        assert runtime.options[0]["github_token"] == _SECRET
        assert runtime.options[0]["use_logged_in_user"] is False
        assert runtime.options[0]["mode"] == "empty"
        assert runtime.options[0]["base_directory"] == runtime.directory
        await provider.status()
        assert runtime.started == 1
        await provider.close()
        await provider.close()
        assert runtime.directory is not None and not Path(runtime.directory).exists()
        assert not (await provider.status()).available

    asyncio.run(exercise())


def test_cli_authentication_requires_explicit_external_login(runtime: Runtime) -> None:
    async def exercise() -> None:
        provider = CopilotProvider(runtime.config)
        runtime.authenticated = False
        status = await provider.status()
        assert status.available and status.authentication == AuthenticationState.REQUIRED
        assert "copilot login" in status.message
        assert runtime.options[0]["use_logged_in_user"] is True
        assert runtime.options[0]["mode"] == "copilot-cli"
        with pytest.raises(ProviderError, match="copilot login"):
            await provider.open_session("app", _context(), _TOOLS)
        assert not runtime.params("session.create")
        runtime.authenticated = True
        assert (await provider.status()).authentication == AuthenticationState.AUTHENTICATED
        await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["start", "status.get", "auth.getStatus"])
def test_readiness_failure_is_explicit_and_never_leaks_vendor_details(
    runtime: Runtime, failure: str
) -> None:
    async def exercise() -> None:
        runtime.fail = failure
        provider = CopilotProvider(runtime.config)
        status = await provider.status()
        assert not status.available and status.authentication == AuthenticationState.UNAVAILABLE
        assert _SECRET not in repr(status)
        assert "failed" in status.message
        await provider.close()
        assert runtime.directory is not None and not Path(runtime.directory).exists()

    asyncio.run(exercise())


def test_released_runtime_version_is_enforced(runtime: Runtime) -> None:
    async def exercise() -> None:
        runtime.runtime_version = "other"
        provider = CopilotProvider(runtime.config)
        assert not (await provider.status()).available
        assert runtime.stopped == 1
        assert not runtime.params("session.create")
        await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("installed", [None, "other"])
def test_missing_or_different_sdk_is_unavailable(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, installed: str | None
) -> None:
    def installed_version(package: str) -> str:
        if installed is None:
            raise PackageNotFoundError(package)
        return installed

    monkeypatch.setattr(adapter, "version", installed_version)

    async def exercise() -> None:
        provider = CopilotProvider(runtime.config)
        assert not (await provider.status()).available
        assert runtime.started == 0
        await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("path", [Path("relative"), Path("/no/such/copilot-runtime")])
def test_runtime_is_never_downloaded_implicitly(runtime: Runtime, path: Path) -> None:
    async def exercise() -> None:
        provider = CopilotProvider(replace(runtime.config, runtime_path=path))
        status = await provider.status()
        assert not status.available and "Provision" in status.message
        assert not runtime.clients
        await provider.close()

    asyncio.run(exercise())


def test_session_configuration_isolated_and_contains_the_complete_snapshot(
    runtime: Runtime,
) -> None:
    async def exercise() -> None:
        provider = CopilotProvider(replace(runtime.config, model="auditor-selected-model"))
        first = await provider.open_session("application-session", _context(), _TOOLS)
        second = await provider.open_session("application-session", _context(), ())
        create = runtime.params("session.create")[0]
        assert first.session_id == second.session_id == "application-session"
        assert create["sessionId"] != runtime.params("session.create")[1]["sessionId"]
        assert runtime.options[0]["mode"] == "copilot-cli"
        assert create["model"] == "auditor-selected-model"
        assert create["availableTools"] == ["pygdb_0", "pygdb_1"]
        assert all(tool["skipPermission"] is True for tool in create["tools"])
        assert runtime.params("session.create")[1]["availableTools"] == []
        content = create["systemMessage"]["content"]
        assert json.loads(content.split("Target snapshot:\n")[1]) == json.loads(
            json.dumps(asdict(_context()))
        )
        assert create["systemMessage"]["mode"] == "replace"
        for key in (
            "enableConfigDiscovery",
            "enableFileHooks",
            "enableHostGitOperations",
            "enableSessionStore",
            "enableSkills",
            "enableSessionTelemetry",
            "enableOnDemandInstructionDiscovery",
        ):
            assert create[key] is False
        assert create["memory"] == {"enabled": False}
        assert create["infiniteSessions"] == {"enabled": False}
        assert create.get("mcpServers", {}) == {}
        assert create.get("customAgents", []) == []
        patch = runtime.params("session.options.update")[1]
        assert patch["skipCustomInstructions"] is True
        assert patch["installedPlugins"] == [] and patch["includedBuiltinSkills"] == []
        sdk_session = runtime.clients[0]._get_session(create["sessionId"])
        assert sdk_session is not None
        assert sdk_session._get_tool_handler("pygdb_0") is None
        assert not any(name == "session.resume" for name, _ in runtime.requests)
        await provider.close()

    asyncio.run(exercise())


def test_streaming_and_correlated_batch_feedback_use_actual_sdk_rpc(runtime: Runtime) -> None:
    async def exercise() -> None:
        provider = CopilotProvider(runtime.config)
        session = await provider.open_session("app", _context(), _TOOLS)
        runtime.turns = [runtime.tools(2), _answer()]
        events = [event async for event in session.stream(UserPrompt("Inspect"))]
        assert events == [
            TextDelta("Inspect safely"),
            ToolCallProposed(ToolCall("call-1", "target_status", {})),
            ToolCallProposed(ToolCall("call-2", "gdb_command", {})),
            TurnFinished("tool_calls"),
        ]
        assert not runtime.params("session.tools.handlePendingToolCall")
        results = ToolResults((ToolResult("call-1", "state"), ToolResult("call-2", "denied", True)))
        assert [event async for event in session.stream(results)] == [
            TextDelta("Stopped"),
            TurnFinished(),
        ]
        feedback = runtime.params("session.tools.handlePendingToolCall")
        assert [item["requestId"] for item in feedback] == ["request-1", "request-2"]
        assert feedback[0]["result"]["textResultForLlm"] == "state"
        assert feedback[1]["result"]["resultType"] == "failure"
        await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["no-results", "wrong-id", "duplicate-id", "new-prompt"])
def test_missing_duplicate_or_uncorrelated_feedback_is_not_sent(
    runtime: Runtime, mode: str
) -> None:
    async def exercise() -> None:
        provider = CopilotProvider(runtime.config)
        session = await provider.open_session("app", _context(), _TOOLS)
        runtime.turns = [runtime.tools()]
        if mode != "no-results":
            _ = [event async for event in session.stream(UserPrompt("Go"))]
        message = (
            UserPrompt("Go")
            if mode == "new-prompt"
            else ToolResults(
                (ToolResult("call-1" if mode == "duplicate-id" else "other", "x"),)
                * (2 if mode == "duplicate-id" else 1)
            )
        )
        with pytest.raises(ProviderError, match="pending"):
            _ = [event async for event in session.stream(message)]
        assert not runtime.params("session.tools.handlePendingToolCall")
        await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "failure", ["session.send", "session.tools.handlePendingToolCall", "reject"]
)
def test_transport_errors_and_rejected_feedback_never_complete(
    runtime: Runtime, failure: str
) -> None:
    async def exercise() -> None:
        provider = CopilotProvider(runtime.config)
        session = await provider.open_session("app", _context(), _TOOLS)
        runtime.turns = [runtime.tools()]
        if failure != "session.send":
            _ = [event async for event in session.stream(UserPrompt("Go"))]
        runtime.fail = failure
        runtime.feedback_success = failure != "reject"
        message = (
            UserPrompt("Go")
            if failure == "session.send"
            else ToolResults((ToolResult("call-1", "x"),))
        )
        with pytest.raises(ProviderError) as caught:
            _ = [event async for event in session.stream(message)]
        assert _SECRET not in str(caught.value)
        assert caught.value.__suppress_context__ or caught.value.__context__ is None
        await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda batch: batch[0][1]["toolRequests"][0].update(name="shell"),
        lambda batch: batch[0][1]["toolRequests"][0].update(toolCallId=""),
        lambda batch: batch[0][1]["toolRequests"][0].update(arguments=[]),
        lambda batch: batch[0][1]["toolRequests"][0].update(arguments={"bad": float("nan")}),
        lambda batch: batch[0][1]["toolRequests"].append(batch[0][1]["toolRequests"][0]),
        lambda batch: batch[0][1].update(chunkCount=2),
        lambda batch: batch.pop(0),
    ],
)
def test_malformed_or_ambient_tools_fail_before_any_proposal(
    runtime: Runtime, mutate: Callable[[list[tuple[str, dict[str, Any]]]], object]
) -> None:
    async def exercise() -> None:
        provider = CopilotProvider(runtime.config)
        session = await provider.open_session("app", _context(), _TOOLS)
        batch = runtime.tools()
        mutate(batch)
        runtime.turns = [batch]
        events = []
        with pytest.raises(ProviderError):
            async for event in session.stream(UserPrompt("Go")):
                events.append(event)
        assert not any(isinstance(event, (ToolCallProposed, TurnFinished)) for event in events)
        assert not runtime.params("session.tools.handlePendingToolCall")
        await provider.close()

    asyncio.run(exercise())


def test_error_events_are_sanitized(runtime: Runtime) -> None:
    async def exercise() -> None:
        provider = CopilotProvider(runtime.config)
        session = await provider.open_session("app", _context(), _TOOLS)
        runtime.turns = [[("session.error", {"errorType": "authentication", "message": _SECRET})]]
        with pytest.raises(ProviderError) as caught:
            _ = [event async for event in session.stream(UserPrompt("Go"))]
        assert _SECRET not in str(caught.value)
        await provider.close()

    asyncio.run(exercise())


def test_sequential_runtime_requests_preserve_complete_plan_and_correlation(
    runtime: Runtime,
) -> None:
    async def exercise() -> None:
        runtime.sequential = True
        provider = CopilotProvider(runtime.config)
        session = await provider.open_session("app", _context(), _TOOLS)
        runtime.turns = [runtime.tools(2), _answer()]
        events = [event async for event in session.stream(UserPrompt("Go"))]
        assert len([event for event in events if isinstance(event, ToolCallProposed)]) == 2
        assert len(runtime.delayed) == 1
        results = ToolResults((ToolResult("call-2", "second"), ToolResult("call-1", "first")))
        async with asyncio.timeout(1):
            events = [event async for event in session.stream(results)]
        assert events[-1] == TurnFinished()
        feedback = runtime.params("session.tools.handlePendingToolCall")
        assert [item["result"]["textResultForLlm"] for item in feedback] == ["first", "second"]
        await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "changes",
    [
        {"toolName": "shell"},
        {"arguments": {"unexpected": True}},
        {"toolCallId": "other"},
        {"requestId": ""},
        {"sessionId": "other"},
    ],
)
def test_external_requests_are_checked_against_the_model_manifest(
    runtime: Runtime, changes: dict[str, Any]
) -> None:
    async def exercise() -> None:
        provider = CopilotProvider(runtime.config)
        session = await provider.open_session("app", _context(), _TOOLS)
        batch = runtime.tools()
        batch[1][1].update(changes)
        runtime.turns = [batch]
        _ = [event async for event in session.stream(UserPrompt("Go"))]
        with pytest.raises(ProviderError, match="match"):
            _ = [event async for event in session.stream(ToolResults((ToolResult("call-1", "x"),)))]
        assert not runtime.params("session.tools.handlePendingToolCall")
        await provider.close()

    asyncio.run(exercise())


def test_json_booleans_cannot_match_numeric_proposal_arguments(runtime: Runtime) -> None:
    async def exercise() -> None:
        provider = CopilotProvider(runtime.config)
        session = await provider.open_session("app", _context(), _TOOLS)
        batch = runtime.tools(arguments={"counter": 1})
        batch[1][1]["arguments"] = {"counter": True}
        runtime.turns = [batch]
        _ = [event async for event in session.stream(UserPrompt("Go"))]
        with pytest.raises(ProviderError, match="match"):
            _ = [event async for event in session.stream(ToolResults((ToolResult("call-1", "x"),)))]
        assert not runtime.params("session.tools.handlePendingToolCall")
        await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["session.idle", "assistant.message"])
def test_runtime_cannot_advance_without_the_pending_results(runtime: Runtime, kind: str) -> None:
    async def exercise() -> None:
        provider = CopilotProvider(runtime.config)
        session = await provider.open_session("app", _context(), _TOOLS)
        batch = runtime.tools()[:1]
        batch.append((kind, {"messageId": "unexpected", "content": "done"}))
        runtime.turns = [batch]
        _ = [event async for event in session.stream(UserPrompt("Go"))]
        with pytest.raises(ProviderError, match="advanced"):
            _ = [event async for event in session.stream(ToolResults((ToolResult("call-1", "x"),)))]
        await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["shell", "read", "write", "mcp", "custom-tool"])
def test_every_sdk_permission_is_denied_independently_of_target_policy(
    runtime: Runtime, kind: str
) -> None:
    async def exercise() -> None:
        provider = CopilotProvider(runtime.config)
        await provider.open_session("app", _context(), _TOOLS)
        sid = runtime.params("session.create")[0]["sessionId"]
        sdk_session = runtime.clients[0]._get_session(sid)
        assert sdk_session is not None
        # This dispatch method accepts the generated union; the callback treats
        # every variant identically and must never inspect or trust its payload.
        await sdk_session._execute_permission_and_respond(
            "permission-1", {"kind": kind}, adapter._deny_permission
        )
        response = runtime.params("session.permissions.handlePendingPermissionRequest")[0]
        assert response["result"]["kind"] == "reject"
        assert not runtime.params("session.tools.handlePendingToolCall")
        await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["native", "subagent", "overflow", "truncated", "mismatched"])
def test_unexpected_or_incomplete_activity_never_completes(runtime: Runtime, kind: str) -> None:
    async def exercise() -> None:
        provider = CopilotProvider(runtime.config)
        session = await provider.open_session("app", _context(), _TOOLS)
        if kind == "native":
            runtime.turns = [
                [("tool.execution_start", {"toolCallId": "native", "toolName": "shell"})]
            ]
        elif kind == "overflow":
            runtime.turns = [
                [("assistant.message_delta", {"messageId": "x", "deltaContent": "x"})] * 257
            ]
        elif kind == "truncated":
            runtime.turns = [[("session.idle", {})]]
        elif kind == "mismatched":
            runtime.turns = [_answer()]
            runtime.turns[0][1][1]["content"] = "different"
        else:
            sid = runtime.params("session.create")[0]["sessionId"]
            runtime.emit(
                sid, "assistant.message", {"messageId": "x", "content": "x"}, agentId="child"
            )
        with pytest.raises(ProviderError):
            _ = [event async for event in session.stream(UserPrompt("Go"))]
        await provider.close()

    asyncio.run(exercise())


def test_rejected_isolation_settings_prevent_exposing_a_session(runtime: Runtime) -> None:
    async def exercise() -> None:
        runtime.isolation_success = False
        provider = CopilotProvider(runtime.config)
        with pytest.raises(ProviderError, match="isolation"):
            await provider.open_session("app", _context(), _TOOLS)
        assert runtime.params("session.detach")
        assert not runtime.params("session.send")
        await provider.close()

    asyncio.run(exercise())


def test_sdk_session_creation_failure_cannot_log_credentials(
    runtime: Runtime, caplog: pytest.LogCaptureFixture
) -> None:
    async def exercise() -> None:
        runtime.fail = "session.create"
        provider = CopilotProvider(runtime.config)
        with pytest.raises(ProviderError, match="session creation") as caught:
            await provider.open_session("app", _context(), _TOOLS)
        assert _SECRET not in str(caught.value)
        assert _SECRET not in caplog.text
        assert "sensitive details suppressed" in caplog.text
        await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["cancel-pending", "close-active", "abandon"])
def test_interrupted_sessions_abort_and_release_their_resources(
    runtime: Runtime, mode: str
) -> None:
    async def exercise() -> None:
        provider = CopilotProvider(runtime.config)
        session = await provider.open_session("app", _context(), _TOOLS)
        if mode == "cancel-pending":
            runtime.turns = [runtime.tools()]
            _ = [event async for event in session.stream(UserPrompt("Go"))]
            await session.cancel()
            with pytest.raises(ProviderError, match="interrupted"):
                await anext(session.stream(UserPrompt("Retry")))
        elif mode == "abandon":
            runtime.turns = [_answer()]
            stream = session.stream(UserPrompt("Go"))
            assert isinstance(stream, AsyncGenerator)
            assert isinstance(await anext(stream), TextDelta)
            await stream.aclose()
        else:
            task = asyncio.ensure_future(anext(session.stream(UserPrompt("Go"))))
            await runtime.sent.wait()
            await session.close()
            with pytest.raises(ProviderCancelledError):
                await task
        assert len(runtime.params("session.abort")) == 1
        await provider.close()
        assert runtime.params("session.detach")

    asyncio.run(exercise())


@pytest.mark.parametrize("approved", [False, True])
def test_copilot_actions_can_only_reach_rpc_through_gateway_approval(
    runtime: Runtime, tmp_path: Path, approved: bool
) -> None:
    async def exercise() -> None:
        with ContextStore(tmp_path / "context.sqlite3") as store:
            store.create_session("app", CopilotProvider.provider_id, _context())
            await run_with_store(store)

    async def run_with_store(store: ContextStore) -> None:
        provider = CopilotProvider(replace(runtime.config, github_token=_SECRET))
        session = await provider.open_session("app", _context(), _TOOLS)
        batch = runtime.tools(2)
        batch[0][1]["toolRequests"][1]["arguments"] = {"command": "info registers"}
        batch[2][1]["arguments"] = {"command": "info registers"}
        runtime.turns = [batch, _answer()]
        responses = {
            "target.context": json.loads(json.dumps(asdict(_context()))),
            "target.status": {"state": "stopped"},
            "command.execute": {"class": "done", "record": "done", "output": ["observed"]},
        }
        approvals = []
        async with FakeRpcServer(responses) as server:
            client = JsonRpcClient(server.url)
            await client.connect()
            try:

                async def approve(request: ApprovalRequest) -> ApprovalResponse:
                    assert not server.requests
                    assert not runtime.params("session.tools.handlePendingToolCall")
                    approvals.append(request)
                    return request.respond(approved=approved)

                gateway = ToolGateway(client, ProtectionPolicy(), approver=approve)
                agent = AgentOrchestrator(
                    session, gateway, _context(), sink=lambda event: None, store=store
                )
                assert (await agent.submit("Inspect the stopped target")).reason == "completed"
                assert len(approvals) == 1
                assert ("command.execute" in server.methods) == approved
                assert ("target.status" in server.methods) == approved
                feedback = runtime.params("session.tools.handlePendingToolCall")
                assert len(feedback) == 2
                expected = "success" if approved else "failure"
                assert all(item["result"]["resultType"] == expected for item in feedback)
                assert _SECRET not in repr(store.history("app"))
                await agent.close()
            finally:
                await client.close()
        await provider.close()

    asyncio.run(exercise())


def test_cancel_text_stream_and_overlap_do_not_poison_next_prompt(runtime: Runtime) -> None:
    async def exercise() -> None:
        provider = CopilotProvider(runtime.config)
        session = await provider.open_session("app", _context(), _TOOLS)
        await session.cancel()
        task = asyncio.ensure_future(anext(session.stream(UserPrompt("Wait"))))
        await runtime.sent.wait()
        with pytest.raises(ProviderError, match="already streaming"):
            await anext(session.stream(UserPrompt("Overlap")))
        await session.cancel()
        with pytest.raises(ProviderCancelledError):
            await task
        runtime.turns = [_answer()]
        assert [event async for event in session.stream(UserPrompt("Retry"))][-1] == TurnFinished()
        await session.close()
        await session.close()
        with pytest.raises(ProviderError, match="closed"):
            await anext(session.stream(UserPrompt("Closed")))
        await provider.close()

    asyncio.run(exercise())


def test_external_cancellation_aborts_runtime_and_propagates(runtime: Runtime) -> None:
    async def exercise() -> None:
        provider = CopilotProvider(runtime.config)
        session = await provider.open_session("app", _context(), _TOOLS)
        task = asyncio.ensure_future(anext(session.stream(UserPrompt("Wait"))))
        await runtime.sent.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(runtime.params("session.abort")) == 1
        await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "key,value",
    [
        ("github_token", ""),
        ("model", " "),
        ("rpc_timeout", 0),
        ("rpc_timeout", True),
        ("rpc_timeout", float("inf")),
        ("rpc_timeout", float("nan")),
    ],
)
def test_invalid_configuration_is_rejected(runtime: Runtime, key: str, value: Any) -> None:
    with pytest.raises(ValueError):
        replace(runtime.config, **{key: value})
