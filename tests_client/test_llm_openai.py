# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Exercise the OpenAI-compatible adapter over an offline transport, without a server."""

import asyncio
from collections.abc import AsyncIterator, Callable
import json
from typing import Any

import httpx
import pytest

from pyGdbClient.llm.contracts import ToolDefinition, ToolResult
from pyGdbClient.llm.openai_compat import OpenAiConfig, OpenAiProvider
from pyGdbClient.llm.provider import (
    AuthenticationState,
    ProviderCancelledError,
    ProviderError,
    ProviderEvent,
    ProviderInput,
    TextDelta,
    ToolCallProposed,
    ToolResults,
    TurnFinished,
    UserPrompt,
)
from test_llm_contracts import _context

_SECRET = "do-not-disclose-prompt-or-server-body"
_TOOLS = (
    ToolDefinition("target_status", "Read known state", {"type": "object"}),
    ToolDefinition("gdb_command", "Propose GDB command", {"type": "object"}),
)

Handler = Callable[[httpx.Request], httpx.Response]


def _provider(handler: Handler, **config: Any) -> OpenAiProvider:
    options = {"api_key": None, "request_timeout": 5.0} | config
    return OpenAiProvider(
        OpenAiConfig("http://server.lan/v1", "test-model", **options),
        transport=httpx.MockTransport(handler),
    )


def _sse(*chunks: object) -> httpx.Response:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def _delta(content: str) -> dict[str, object]:
    return {"choices": [{"delta": {"content": content}}]}


def _fragment(index: int, call_id: str = "", name: str = "", arguments: str = "") -> dict[str, Any]:
    item: dict[str, Any] = {"index": index, "function": {}}
    if call_id:
        item["id"] = call_id
    if name:
        item["function"]["name"] = name
    if arguments:
        item["function"]["arguments"] = arguments
    return {"choices": [{"delta": {"tool_calls": [item]}}]}


def _finish(reason: str) -> dict[str, object]:
    return {"choices": [{"delta": {}, "finish_reason": reason}]}


def _text_turn(text: str) -> httpx.Response:
    return _sse(_delta(text), _finish("stop"))


class _Script:
    """Serve /models then scripted completion responses, recording every request."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": []})
        assert request.url.path == "/v1/chat/completions"
        assert request.method == "POST"
        self.bodies.append(json.loads(request.content))
        return self.responses.pop(0)


async def _events(session: Any, message: ProviderInput) -> list[ProviderEvent]:
    return [event async for event in session.stream(message)]


@pytest.mark.parametrize(
    "options",
    [
        {"base_url": "ftp://server/v1"},
        {"base_url": "http:///v1"},
        {"model": " "},
        {"api_key": " "},
        {"request_timeout": 0.0},
        {"request_timeout": float("nan")},
        {"request_timeout": True},
    ],
)
def test_config_rejects_unusable_endpoint_credential_or_timeout(options: dict[str, Any]) -> None:
    """Misconfiguration fails at construction, not during an audit turn."""
    values: dict[str, Any] = {"base_url": "http://server.lan/v1", "model": "m"} | options
    with pytest.raises(ValueError):
        OpenAiConfig(**values)


def test_api_key_is_excluded_from_representation() -> None:
    """The in-memory credential never appears in reprs or logs built from them."""
    config = OpenAiConfig("http://server.lan/v1", "m", api_key=_SECRET)
    assert _SECRET not in repr(config)


def test_status_reports_reachability_and_anonymous_access() -> None:
    """A reachable keyless endpoint requires no authentication."""

    async def exercise() -> None:
        script = _Script()
        provider = _provider(script)
        status = await provider.status()
        assert status.available is True
        assert status.authentication is AuthenticationState.NOT_REQUIRED
        assert "authorization" not in script.requests[0].headers
        await provider.close()

    asyncio.run(exercise())


def test_status_sends_the_bearer_key_and_reports_acceptance() -> None:
    """A configured key is sent as a bearer token and acceptance is reported."""

    async def exercise() -> None:
        script = _Script()
        provider = _provider(script, api_key="key-1")
        status = await provider.status()
        assert status.authentication is AuthenticationState.AUTHENTICATED
        assert script.requests[0].headers["authorization"] == "Bearer key-1"
        await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("code", [401, 403])
@pytest.mark.parametrize("key", [None, "key-1"])
def test_rejected_credentials_are_reported_without_the_server_body(
    code: int, key: str | None
) -> None:
    """Authentication guidance never echoes the endpoint's response body."""

    async def exercise() -> None:
        provider = _provider(lambda request: httpx.Response(code, text=_SECRET), api_key=key)
        status = await provider.status()
        assert status.available is True
        expected = AuthenticationState.REQUIRED if key is None else AuthenticationState.EXPIRED
        assert status.authentication is expected
        assert _SECRET not in status.message
        await provider.close()

    asyncio.run(exercise())


def test_unreachable_or_failing_endpoint_is_unavailable_with_neutral_guidance() -> None:
    """Transport failures and unexpected statuses are sanitized, not raised."""

    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(_SECRET)

    async def exercise() -> None:
        provider = _provider(failing)
        status = await provider.status()
        assert status.available is False
        assert status.authentication is AuthenticationState.UNAVAILABLE
        assert _SECRET not in status.message
        await provider.close()
        provider = _provider(lambda request: httpx.Response(503, text=_SECRET))
        status = await provider.status()
        assert status.available is False and "503" in status.message
        assert _SECRET not in status.message
        await provider.close()

    asyncio.run(exercise())


def test_open_session_requires_identifiers_unique_api_safe_tools_and_access() -> None:
    """Session preconditions fail explicitly before any completion request."""

    async def exercise() -> None:
        provider = _provider(_Script())
        with pytest.raises(ValueError, match="nonempty ID"):
            await provider.open_session(" ", _context(), _TOOLS)
        with pytest.raises(ValueError, match="unique"):
            await provider.open_session("s", _context(), (_TOOLS[0], _TOOLS[0]))
        bad = ToolDefinition("bad name!", "x", {"type": "object"})
        with pytest.raises(ValueError, match="not accepted"):
            await provider.open_session("s", _context(), (bad,))
        await provider.close()
        rejecting = _provider(lambda request: httpx.Response(401))
        with pytest.raises(ProviderError, match="--openai-key-env"):
            await rejecting.open_session("s", _context(), _TOOLS)
        await rejecting.close()

    asyncio.run(exercise())


def test_streamed_turns_replay_the_history_with_snapshot_and_tools() -> None:
    """Each request carries the system snapshot, prior turns and tool schemas."""

    async def exercise() -> None:
        script = _Script(_text_turn("first"), _text_turn("second"))
        provider = _provider(script)
        session = await provider.open_session("s1", _context(), _TOOLS)
        events = await _events(session, UserPrompt("hello"))
        assert events == [TextDelta("first"), TurnFinished()]
        events = await _events(session, UserPrompt("again"))
        assert events == [TextDelta("second"), TurnFinished()]
        body = script.bodies[-1]
        assert body["model"] == "test-model"
        assert body["stream"] is True
        assert [tool["function"]["name"] for tool in body["tools"]] == [
            "target_status",
            "gdb_command",
        ]
        roles = [message["role"] for message in body["messages"]]
        assert roles == ["system", "user", "assistant", "user"]
        assert "target-1:snapshot-1" in body["messages"][0]["content"]
        assert body["messages"][2]["content"] == "first"
        await provider.close()

    asyncio.run(exercise())


def test_tool_batch_is_assembled_from_fragments_and_fed_back_correlated() -> None:
    """Fragmented tool-call deltas form one proposed batch; results return as tool roles."""

    async def exercise() -> None:
        script = _Script(
            _sse(
                _delta("Plan"),
                _fragment(0, call_id="call-1", name="target_status"),
                _fragment(0, arguments='{"timeo'),
                _fragment(0, arguments='ut": 5}'),
                _fragment(1, call_id="call-2", name="gdb_command", arguments="{}"),
                _finish("tool_calls"),
            ),
            _text_turn("done"),
        )
        provider = _provider(script)
        session = await provider.open_session("s1", _context(), _TOOLS)
        events = await _events(session, UserPrompt("audit"))
        calls = [event.call for event in events if isinstance(event, ToolCallProposed)]
        assert [call.call_id for call in calls] == ["call-1", "call-2"]
        assert calls[0].arguments == {"timeout": 5}
        assert events[0] == TextDelta("Plan")
        assert events[-1] == TurnFinished("tool_calls")
        with pytest.raises(ProviderError, match="pending"):
            await _events(session, UserPrompt("ignore the batch"))
        results = ToolResults(
            (ToolResult("call-1", "halted"), ToolResult("call-2", "refused", is_error=True))
        )
        events = await _events(session, results)
        assert events == [TextDelta("done"), TurnFinished()]
        messages = script.bodies[-1]["messages"]
        assert messages[-2]["tool_call_id"] == "call-1"
        assert messages[-2]["content"] == "halted"
        assert messages[-1]["tool_call_id"] == "call-2"
        assert "failed" in messages[-1]["content"] and "refused" in messages[-1]["content"]
        recorded = messages[-3]
        assert recorded["role"] == "assistant"
        assert [call["id"] for call in recorded["tool_calls"]] == ["call-1", "call-2"]
        await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["no-results", "wrong-id", "duplicate-id", "missing-one"])
def test_mismatched_feedback_is_rejected_before_any_request(mode: str) -> None:
    """Results must match the pending batch exactly; nothing reaches the endpoint."""

    async def exercise() -> None:
        script = _Script(
            _sse(_fragment(0, call_id="call-1", name="target_status"), _finish("tool_calls"))
        )
        provider = _provider(script)
        session = await provider.open_session("s1", _context(), _TOOLS)
        await _events(session, UserPrompt("audit"))
        results = {
            "no-results": (),
            "wrong-id": (ToolResult("other", "x"),),
            "duplicate-id": (ToolResult("call-1", "x"), ToolResult("call-1", "x")),
            "missing-one": (ToolResult("call-1", "x"), ToolResult("extra", "x")),
        }[mode]
        requests = len(script.requests)
        with pytest.raises(ProviderError, match="pending"):
            await _events(session, ToolResults(results))
        assert len(script.requests) == requests
        await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "turn",
    [
        _sse(_fragment(0, call_id="call-1", name="unknown_tool"), _finish("tool_calls")),
        _sse(_fragment(0, name="target_status"), _finish("tool_calls")),
        _sse(_fragment(0, call_id="call-1", name="target_status", arguments="{"),
             _finish("tool_calls")),
        _sse(_fragment(0, call_id="call-1", name="target_status", arguments="[]"),
             _finish("tool_calls")),
        _sse(_finish("tool_calls")),
        _sse(_delta("truncated"), _finish("length")),
        _sse(_delta("no finish reason")),
        _sse(_delta("before failure"), "not-a-json-object"),
        httpx.Response(500, text=_SECRET),
    ],
)
def test_invalid_proposals_and_failed_completions_never_finish(turn: httpx.Response) -> None:
    """Malformed model output is an explicit sanitized error, not a completed turn."""

    async def exercise() -> None:
        script = _Script(turn, _text_turn("recovered"))
        provider = _provider(script)
        session = await provider.open_session("s1", _context(), _TOOLS)
        with pytest.raises(ProviderError) as failure:
            await _events(session, UserPrompt(_SECRET))
        assert _SECRET not in str(failure.value)
        assert not isinstance(failure.value, ProviderCancelledError)
        # The failed turn left no partial history behind.
        events = await _events(session, UserPrompt("retry"))
        assert events == [TextDelta("recovered"), TurnFinished()]
        roles = [message["role"] for message in script.bodies[-1]["messages"]]
        assert roles == ["system", "user"]
        await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "code,body,expected",
    [
        (
            404,
            {"error": {"message": f"model '{_SECRET}' not found"}},
            "The selected model was not found",
        ),
        (
            404,
            {"error": f"model '{_SECRET}' not found"},
            "The selected model was not found",
        ),
        (404, {"error": {"message": _SECRET}}, "including /v1"),
        (
            400,
            {"error": {"message": f"{_SECRET} does not support tools"}},
            "The selected model does not support tool calling",
        ),
        (400, {"error": f"{_SECRET} does not support tools"}, "llama3.2:latest"),
        (400, {"error": {"message": _SECRET}}, "HTTP 400"),
        (401, {"error": _SECRET}, "--openai-key-env"),
        (403, {"error": _SECRET}, "--openai-key-env"),
        (500, {"error": _SECRET}, "HTTP 500"),
    ],
)
def test_completion_errors_explain_configuration_without_disclosing_server_text(
    code: int, body: dict[str, Any], expected: str
) -> None:
    """Missing models and unsupported tools are distinct from an absent API route."""

    async def exercise() -> None:
        script = _Script(httpx.Response(code, json=body), _text_turn("recovered"))
        provider = _provider(script)
        try:
            session = await provider.open_session("s1", _context(), _TOOLS)
            with pytest.raises(ProviderError, match=expected) as failure:
                await _events(session, UserPrompt(_SECRET))
            assert _SECRET not in str(failure.value)
            assert len(script.bodies) == 1
            assert script.bodies[0]["tools"]
            events = await _events(session, UserPrompt("retry"))
            assert events == [TextDelta("recovered"), TurnFinished()]
            assert [message["role"] for message in script.bodies[-1]["messages"]] == [
                "system",
                "user",
            ]
        finally:
            await provider.close()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "body",
    [
        b"404 page not found",
        b"\xff",
        b'{"error":{"message":"does not support tools"}}' + b"x" * 5000,
        b"[]",
    ],
)
def test_unrecognized_error_bodies_remain_sanitized(body: bytes) -> None:
    """Non-JSON, oversized and unexpected server bodies do not escape the adapter."""

    async def exercise() -> None:
        script = _Script(httpx.Response(404, content=body))
        provider = _provider(script)
        try:
            session = await provider.open_session("s1", _context(), _TOOLS)
            with pytest.raises(ProviderError, match="including /v1"):
                await _events(session, UserPrompt("hello"))
        finally:
            await provider.close()

    asyncio.run(exercise())


def test_error_body_reads_are_bounded_and_response_is_closed() -> None:
    """A streaming error response cannot make diagnostics read unlimited server data."""

    class ErrorStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.reads = 0
            self.closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(10):
                self.reads += 1
                yield b"x" * 1024

        async def aclose(self) -> None:
            self.closed = True

    async def exercise() -> None:
        stream = ErrorStream()
        provider = _provider(_Script(httpx.Response(404, stream=stream)))
        try:
            session = await provider.open_session("s1", _context(), _TOOLS)
            with pytest.raises(ProviderError, match="HTTP 404"):
                await _events(session, UserPrompt("hello"))
            assert stream.reads == 4
            assert stream.closed
        finally:
            await provider.close()

    asyncio.run(exercise())


def test_reused_call_ids_across_turns_are_rejected() -> None:
    """A recycled tool call ID cannot inherit an earlier correlation or approval."""

    async def exercise() -> None:
        batch = _sse(_fragment(0, call_id="call-1", name="target_status"), _finish("tool_calls"))
        script = _Script(
            batch,
            _sse(_fragment(0, call_id="call-1", name="target_status"), _finish("tool_calls")),
        )
        provider = _provider(script)
        session = await provider.open_session("s1", _context(), _TOOLS)
        await _events(session, UserPrompt("audit"))
        with pytest.raises(ProviderError, match="reused"):
            await _events(session, ToolResults((ToolResult("call-1", "halted"),)))
        await provider.close()

    asyncio.run(exercise())


def test_failed_feedback_turn_keeps_the_pending_batch_for_a_retry() -> None:
    """A transport failure while reporting results does not lose the pending batch."""

    async def exercise() -> None:
        script = _Script(
            _sse(_fragment(0, call_id="call-1", name="target_status"), _finish("tool_calls")),
            httpx.Response(500),
            _text_turn("recovered"),
        )
        provider = _provider(script)
        session = await provider.open_session("s1", _context(), _TOOLS)
        await _events(session, UserPrompt("audit"))
        results = ToolResults((ToolResult("call-1", "halted"),))
        with pytest.raises(ProviderError, match="HTTP 500"):
            await _events(session, results)
        events = await _events(session, results)
        assert events == [TextDelta("recovered"), TurnFinished()]
        assert script.bodies[-1]["messages"][-1]["role"] == "tool"
        await provider.close()

    asyncio.run(exercise())


def test_cancellation_interrupts_a_hanging_stream_and_rolls_back() -> None:
    """Cancel stops the turn explicitly; the next turn starts from a clean history."""

    class HangingStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.release = asyncio.Event()

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            await self.release.wait()

        async def aclose(self) -> None:
            self.release.set()

    responses: list[httpx.Response] = [
        httpx.Response(200, json={"data": []}),
        httpx.Response(200, stream=HangingStream()),
        _text_turn("after cancel"),
    ]
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            bodies.append(json.loads(request.content))
        return responses.pop(0)

    async def exercise() -> None:
        provider = _provider(handler)
        session = await provider.open_session("s1", _context(), _TOOLS)
        received: list[ProviderEvent] = []
        with pytest.raises(ProviderCancelledError):
            async for event in session.stream(UserPrompt("hello")):
                received.append(event)
                await session.cancel()
        assert received == [TextDelta("partial")]
        events = await _events(session, UserPrompt("retry"))
        assert events == [TextDelta("after cancel"), TurnFinished()]
        assert [message["role"] for message in bodies[-1]["messages"]] == ["system", "user"]
        await provider.close()

    asyncio.run(exercise())


def test_slow_reads_time_out_explicitly() -> None:
    """A stalled endpoint is a bounded sanitized failure, not an endless wait."""

    class StalledStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            await asyncio.sleep(3600)
            yield b""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, stream=StalledStream())

    async def exercise() -> None:
        provider = _provider(handler, request_timeout=0.05)
        session = await provider.open_session("s1", _context(), _TOOLS)
        with pytest.raises(ProviderError, match="timed out"):
            await _events(session, UserPrompt("hello"))
        await provider.close()

    asyncio.run(exercise())


def test_concurrent_turns_are_rejected_and_close_is_idempotent() -> None:
    """One turn streams at a time; closed sessions and providers reject further use."""

    async def exercise() -> None:
        script = _Script(_text_turn("only"))
        provider = _provider(script)
        session = await provider.open_session("s1", _context(), _TOOLS)
        stream = session.stream(UserPrompt("hello"))
        assert isinstance(await anext(stream), TextDelta)
        with pytest.raises(ProviderError, match="already streaming"):
            await _events(session, UserPrompt("concurrent"))
        await stream.aclose()
        await session.close()
        await session.close()
        with pytest.raises(ProviderError, match="closed"):
            await _events(session, UserPrompt("after close"))
        await provider.close()
        await provider.close()
        with pytest.raises(ProviderError, match="closed"):
            await provider.open_session("s2", _context(), _TOOLS)
        assert (await provider.status()).available is False

    asyncio.run(exercise())


def test_missing_httpx_dependency_is_explicit_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the optional extra the provider reports installation guidance."""
    import builtins

    original = builtins.__import__

    def reject_httpx(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "httpx":
            raise ImportError("httpx is not installed")
        return original(name, *args, **kwargs)

    async def exercise() -> None:
        monkeypatch.setattr(builtins, "__import__", reject_httpx)
        provider = OpenAiProvider(OpenAiConfig("http://server.lan/v1", "m"))
        status = await provider.status()
        assert status.available is False
        assert "pyGdbToolkit[openai]" in status.message
        await provider.close()

    asyncio.run(exercise())
