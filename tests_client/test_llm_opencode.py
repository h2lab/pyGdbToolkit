# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""OpenCode V2 integration over an offline HTTP transport and a fake target."""

import asyncio
from dataclasses import asdict
import json
from typing import Any

import httpx
import pytest

from pyGdbClient.llm.approval import ApprovalRequest, ApprovalResponse
from pyGdbClient.llm.contracts import ToolCall, ToolDefinition, ToolResult
from pyGdbClient.llm.controller import AgentController, AgentSettings
from pyGdbClient.llm.opencode import OpenCodeConfig, OpenCodeProvider
from pyGdbClient.llm.provider import (
    AuthenticationState,
    ProviderCancelledError,
    ProviderError,
    TextDelta,
    ToolCallProposed,
    ToolResults,
    TurnFinished,
    UserPrompt,
)
from pyGdbClient.rpc import JsonRpcClient
from rpc_fakes import FakeRpcServer
from test_llm_contracts import _context

DENY = [{"action": "*", "resource": "*", "effect": "deny"}]
TOOLS = (ToolDefinition("gdb_command", "Execute with approval", {"type": "object"}),)
SECRET = "server-response-secret"


class Server:
    """Script the documented V2 session and transient generation endpoints."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []
        self.prompts: list[str] = []
        self.permissions: object = DENY

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/info":
            return httpx.Response(200, json={"version": "2.0.0"})
        if request.url.path == "/api/session" and request.method == "POST":
            body = json.loads(request.content)
            assert body["permissions"] == DENY
            return httpx.Response(
                200, json={"data": {"id": "ses_123", "permissions": self.permissions}}
            )
        if request.url.path == "/api/session/ses_123/generate":
            self.prompts.append(json.loads(request.content)["prompt"])
            return httpx.Response(200, json={"data": {"text": self.answers.pop(0)}})
        if request.url.path == "/api/session/ses_123" and request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")


def provider(server: Server, **options: Any) -> OpenCodeProvider:
    return OpenCodeProvider(
        OpenCodeConfig("http://server:4096", request_timeout=5, **options),
        httpx.MockTransport(server),
    )


def answer(text: str) -> str:
    return json.dumps({"type": "answer", "text": text})


def calls(call_id: str = "c1", name: str = "gdb_command") -> str:
    return json.dumps(
        {
            "type": "tool_calls",
            "calls": [{"id": call_id, "name": name, "arguments": {"command": "continue"}}],
        }
    )


@pytest.mark.parametrize(
    "values",
    [
        {"base_url": "ftp://server"},
        {"base_url": "http://user:password@server"},
        {"base_url": "http://server?token=secret"},
        {"model": "llama"},
        {"model": "ollama/llama#"},
        {"model": "ollama/llama#fast#extra"},
        {"agent": " "},
        {"password": " "},
        {"request_timeout": 0},
        {"request_timeout": float("inf")},
        {"request_timeout": True},
    ],
)
def test_invalid_configuration(values: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        OpenCodeConfig(**({"base_url": "http://server"} | values))


def test_session_uses_restricted_v2_generation_and_local_history() -> None:
    async def run() -> None:
        server = Server(calls(), answer("Target stopped."))
        backend = provider(server, agent="audit", model="ollama/llama3.2:latest", password="pw")
        status = await backend.status()
        assert status.authentication == AuthenticationState.AUTHENTICATED
        assert server.requests[0].headers["authorization"] == "Basic b3BlbmNvZGU6cHc="
        session = await backend.open_session(
            "client-id", _context(), TOOLS, {"project": {"name": "sample"}}
        )
        create = json.loads(
            next(r for r in server.requests if r.url.path == "/api/session").content
        )
        assert create["agent"] == "audit"
        assert create["model"] == {"providerID": "ollama", "id": "llama3.2:latest"}
        events = [event async for event in session.stream(UserPrompt("Check target"))]
        assert events == [
            ToolCallProposed(ToolCall("c1", "gdb_command", {"command": "continue"})),
            TurnFinished("tool_calls"),
        ]
        result = ToolResult("c1", '{"status":"approved"}')
        events = [event async for event in session.stream(ToolResults((result,)))]
        assert events == [TextDelta("Target stopped."), TurnFinished()]
        assert "target-1:snapshot-1" in server.prompts[0]
        assert '"name": "sample"' in server.prompts[0]
        assert '"gdb_command"' in server.prompts[0]
        assert '"call_id": "c1"' in server.prompts[1]
        assert '"status\\":\\"approved' in server.prompts[1]
        assert "secret" not in repr(backend._config)
        await session.close()
        await session.close()
        await backend.close()
        assert sum(r.method == "DELETE" for r in server.requests) == 1

    asyncio.run(run())


def test_model_variant_is_sent_as_a_separate_v2_field() -> None:
    async def run() -> None:
        server = Server()
        backend = provider(server, model="ollama/model#fast")
        await backend.open_session("s", _context(), TOOLS)
        request = next(r for r in server.requests if r.url.path == "/api/session")
        assert json.loads(request.content)["model"] == {
            "providerID": "ollama",
            "id": "model",
            "variant": "fast",
        }
        await backend.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "invalid",
    [
        "unstructured answer",
        '{"type":"answer","text":"one","text":"two"}',
        '{"type":"answer","text":""}',
        '{"type":"answer","text":"x","extra":1}',
        '{"type":"tool_calls","calls":[]}',
        '{"type":"tool_calls","calls":[{"id":"c1","name":"unknown","arguments":{}}]}',
        '{"type":"tool_calls","calls":[{"id":"c1","name":"gdb_command","arguments":[] }]}',
        '{"type":"tool_calls","calls":[{"id":"c1","name":"gdb_command","arguments":{"n":NaN}}]}',
        '{"type":"tool_calls","calls":[{"id":"c1","name":"gdb_command","arguments":{}},'
        '{"id":"c1","name":"gdb_command","arguments":{}}]}',
    ],
)
def test_malformed_model_responses_fail_without_fallback(invalid: str) -> None:
    async def run() -> None:
        server = Server(invalid, answer("retry"))
        backend = provider(server)
        session = await backend.open_session("s", _context(), TOOLS)
        with pytest.raises(ProviderError) as error:
            _ = [event async for event in session.stream(UserPrompt(SECRET))]
        assert SECRET not in str(error.value)
        assert [event async for event in session.stream(UserPrompt("again"))] == [
            TextDelta("retry"),
            TurnFinished(),
        ]
        assert SECRET not in server.prompts[1]
        await backend.close()

    asyncio.run(run())


def test_pending_results_and_reused_ids_are_rejected() -> None:
    async def run() -> None:
        server = Server(calls(), calls())
        backend = provider(server)
        session = await backend.open_session("s", _context(), TOOLS)
        _ = [event async for event in session.stream(UserPrompt("go"))]
        with pytest.raises(ProviderError, match="pending"):
            _ = [event async for event in session.stream(UserPrompt("again"))]
        with pytest.raises(ProviderError, match="one result"):
            _ = [event async for event in session.stream(ToolResults((ToolResult("wrong", "x"),)))]
        with pytest.raises(ProviderError, match="repeated"):
            _ = [event async for event in session.stream(ToolResults((ToolResult("c1", "x"),)))]
        await backend.close()

    asyncio.run(run())


def test_unconfirmed_permissions_cannot_open_a_session() -> None:
    async def run() -> None:
        server = Server()
        server.permissions = []
        backend = provider(server)
        with pytest.raises(ProviderError, match="restrictions"):
            await backend.open_session("s", _context(), TOOLS)
        assert not server.prompts
        assert any(request.method == "DELETE" for request in server.requests)
        await backend.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text=SECRET),
        httpx.Response(200, text=SECRET),
        httpx.Response(200, json={"data": {"text": None}}),
        httpx.Response(200, content=b"x" * (1024 * 1024 + 1)),
    ],
)
def test_generation_errors_are_explicit_and_sanitized(response: httpx.Response) -> None:
    async def run() -> None:
        server = Server()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/generate"):
                return response
            return server(request)

        backend = OpenCodeProvider(OpenCodeConfig("http://server"), httpx.MockTransport(handler))
        session = await backend.open_session("s", _context(), TOOLS)
        with pytest.raises(ProviderError) as error:
            _ = [event async for event in session.stream(UserPrompt("go"))]
        assert SECRET not in str(error.value)
        await backend.close()

    asyncio.run(run())


def test_server_transport_errors_are_sanitized() -> None:
    async def run() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(SECRET)

        backend = OpenCodeProvider(OpenCodeConfig("http://server"), httpx.MockTransport(handler))
        status = await backend.status()
        assert not status.available and SECRET not in status.message
        await backend.close()

    asyncio.run(run())


def test_remote_session_deletion_failure_is_not_hidden() -> None:
    async def run() -> None:
        server = Server()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE":
                return httpx.Response(503, text=SECRET)
            return server(request)

        backend = OpenCodeProvider(OpenCodeConfig("http://server"), httpx.MockTransport(handler))
        session = await backend.open_session("s", _context(), TOOLS)
        with pytest.raises(ProviderError, match="503") as error:
            await session.close()
        assert SECRET not in str(error.value)
        with pytest.raises(ProviderError, match="503"):
            await backend.close()

    asyncio.run(run())


@pytest.mark.parametrize("code", [401, 403, 404, 500])
def test_status_is_sanitized(code: int) -> None:
    async def run() -> None:
        backend = OpenCodeProvider(
            OpenCodeConfig("http://server"),
            httpx.MockTransport(lambda request: httpx.Response(code, text=SECRET)),
        )
        status = await backend.status()
        assert SECRET not in status.message
        if code in (401, 403):
            assert status.authentication == AuthenticationState.REQUIRED
        else:
            assert not status.available
        await backend.close()

    asyncio.run(run())


def test_cancel_interrupts_the_local_generation_request() -> None:
    async def run() -> None:
        server = Server()
        started = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/generate"):
                started.set()
                await asyncio.Event().wait()
            return server(request)

        backend = OpenCodeProvider(
            OpenCodeConfig("http://server", request_timeout=5), httpx.MockTransport(handler)
        )
        session = await backend.open_session("s", _context(), TOOLS)

        async def consume() -> None:
            _ = [event async for event in session.stream(UserPrompt("go"))]

        task = asyncio.create_task(consume())
        await started.wait()
        await session.cancel()
        with pytest.raises(ProviderCancelledError):
            await task
        await backend.close()

    asyncio.run(run())


@pytest.mark.parametrize("approve", [True, False])
def test_tool_proposals_pass_through_real_gateway_and_approval(approve: bool) -> None:
    async def run() -> None:
        open_code = Server(calls(), answer("Done."))
        backend = provider(open_code)
        approvals: list[ApprovalRequest] = []

        async def decide(request: ApprovalRequest) -> ApprovalResponse:
            approvals.append(request)
            return request.respond(approved=approve)

        responses = {
            "target.context": asdict(_context()),
            "target.status": {"state": "stopped"},
            "command.execute": {"class": "done", "record": "done", "output": ["continued"]},
        }
        async with FakeRpcServer(responses) as target:
            client = JsonRpcClient(target.url)
            await client.connect()
            controller = AgentController(
                AgentSettings(backend), client, approver=decide, sink=lambda event: None
            )
            result = await controller.submit("Continue the target")
            assert result.reason == "completed"
            assert len(approvals) == 1
            assert ("command.execute" in target.methods) is approve
            assert '"is_error": ' + ("false" if approve else "true") in open_code.prompts[-1]
            await controller.close()
            await client.close()

    asyncio.run(run())
