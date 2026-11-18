# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Optional OpenAI-compatible adapter for LAN inference servers such as Ollama or llama.cpp.

The adapter speaks the stateless ``/v1/chat/completions`` streaming API: the
session owns the conversation history locally and replays it on every turn.
Target operations remain declaration-only; proposed tool calls are never
executed here.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable
import contextlib
from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
import re
from urllib.parse import urlsplit

from ._http import SSE_DONE, AsyncBaseTransport, AsyncClient, finite_json, open_client, sse_data
from ._http import step as transport_step
from .contracts import JsonObject, JsonValue, TargetContext, ToolCall, ToolDefinition
from .instructions import agent_instructions
from .provider import (
    AuthenticationState,
    LlmSession,
    ProviderError,
    ProviderEvent,
    ProviderInput,
    ProviderStatus,
    TextDelta,
    ToolCallProposed,
    ToolResults,
    TurnFinished,
    UserPrompt,
)

_KEY_GUIDANCE = "Supply an API key with --openai-key-env, or allow anonymous access on the server."
_INSTALL_GUIDANCE = "Install pyGdbToolkit[openai] to enable the OpenAI-compatible provider."
_LABEL = "OpenAI-compatible"
_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MAX_ERROR_BODY_BYTES = 4096


@dataclass(frozen=True)
class OpenAiConfig:
    """Explicit endpoint, model and optional in-memory credential."""

    base_url: str
    model: str
    api_key: str | None = field(default=None, repr=False)
    request_timeout: float = 120.0

    def __post_init__(self) -> None:
        """Reject unusable endpoints, ambiguous credentials and unbounded waits."""
        parts = urlsplit(self.base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        if not self.model.strip():
            raise ValueError("model must be nonempty")
        if self.api_key is not None and not self.api_key.strip():
            raise ValueError("api_key must be nonempty when supplied")
        if (
            isinstance(self.request_timeout, bool)
            or not math.isfinite(self.request_timeout)
            or self.request_timeout <= 0
        ):
            raise ValueError("request_timeout must be finite and positive")


class OpenAiProvider:
    """Own one HTTP client for an OpenAI-compatible endpoint, without target access."""

    provider_id = "openai"

    def __init__(self, config: OpenAiConfig, transport: AsyncBaseTransport | None = None) -> None:
        """Keep the configuration; the first operation creates the HTTP client.

        Parameters
        ----------
        config : OpenAiConfig
            Endpoint, model, optional credential and per-step timeout.
        transport : httpx.AsyncBaseTransport, optional
            Replacement transport for offline tests; production uses the default.
        """
        self._config = config
        self._transport = transport
        self._client: AsyncClient | None = None
        self._sessions: list[OpenAiLlmSession] = []
        self._lock = asyncio.Lock()
        self._closed = False

    async def _connect(self) -> AsyncClient:
        async with self._lock:
            if self._closed:
                raise ProviderError("OpenAI provider is closed")
            if self._client is not None:
                return self._client
            headers: dict[str, str] = {}
            if self._config.api_key is not None:
                headers["Authorization"] = f"Bearer {self._config.api_key}"
            self._client = open_client(
                self._config.base_url,
                headers,
                self._config.request_timeout,
                self._transport,
                _INSTALL_GUIDANCE,
            )
            return self._client

    async def status(self) -> ProviderStatus:
        """Probe endpoint reachability and credential acceptance, without inference."""
        try:
            client = await self._connect()
        except ProviderError as error:
            return ProviderStatus(False, AuthenticationState.UNAVAILABLE, str(error))
        try:
            async with asyncio.timeout(self._config.request_timeout):
                response = await client.get("models")
        except Exception:
            # Vendor errors can echo request details; report only neutral guidance.
            return ProviderStatus(
                False,
                AuthenticationState.UNAVAILABLE,
                "OpenAI-compatible endpoint is unreachable; check --openai-url and the server.",
            )
        if response.status_code in (401, 403):
            if self._config.api_key is None:
                return ProviderStatus(True, AuthenticationState.REQUIRED, _KEY_GUIDANCE)
            return ProviderStatus(
                True, AuthenticationState.EXPIRED, "The endpoint rejected the configured API key."
            )
        if response.status_code != 200:
            return ProviderStatus(
                False,
                AuthenticationState.UNAVAILABLE,
                f"OpenAI-compatible endpoint returned HTTP {response.status_code}.",
            )
        if self._config.api_key is None:
            return ProviderStatus(
                True, AuthenticationState.NOT_REQUIRED, "OpenAI-compatible endpoint is reachable."
            )
        return ProviderStatus(
            True, AuthenticationState.AUTHENTICATED, "OpenAI-compatible endpoint accepted the key."
        )

    async def open_session(
        self,
        session_id: str,
        context: TargetContext,
        tools: tuple[ToolDefinition, ...],
        audit: JsonObject | None = None,
    ) -> LlmSession:
        """Create a fresh local conversation seeded with the target snapshot."""
        if not session_id.strip():
            raise ValueError("sessions require a nonempty ID")
        if len({tool.name for tool in tools}) != len(tools):
            raise ValueError("tool names must be unique")
        for tool in tools:
            if not _TOOL_NAME.fullmatch(tool.name):
                raise ValueError(f"tool name is not accepted by the API: {tool.name!r}")
        status = await self.status()
        if not status.available or status.authentication not in (
            AuthenticationState.NOT_REQUIRED,
            AuthenticationState.AUTHENTICATED,
        ):
            raise ProviderError(status.message)
        client = await self._connect()
        async with self._lock:
            if self._closed:
                raise ProviderError("OpenAI provider is closed")
            session = OpenAiLlmSession(
                session_id, client, self._config, tools, agent_instructions(context, audit)
            )
            self._sessions.append(session)
            return session

    async def close(self) -> None:
        """Close owned sessions and the HTTP client idempotently."""
        async with self._lock:
            self._closed = True
            try:
                for session in self._sessions:
                    await session.close()
            finally:
                client, self._client = self._client, None
                if client is not None:
                    with contextlib.suppress(Exception):
                        await client.aclose()


class OpenAiLlmSession:
    """Replay a locally owned message history over stateless streaming completions."""

    def __init__(
        self,
        session_id: str,
        client: AsyncClient,
        config: OpenAiConfig,
        tools: tuple[ToolDefinition, ...],
        instructions: str,
    ) -> None:
        """Seed the history with the system instructions and declared tool schemas."""
        self.session_id = session_id
        self._client = client
        self._model = config.model
        self._timeout = config.request_timeout
        self._names = {tool.name for tool in tools}
        self._tools: list[JsonValue] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": deepcopy(tool.input_schema),
                },
            }
            for tool in tools
        ]
        self._messages: list[JsonValue] = [{"role": "system", "content": instructions}]
        self._pending: dict[str, ToolCall] = {}
        self._seen: set[str] = set()
        self._active = False
        self._closed = False
        self._cancel_event = asyncio.Event()

    async def _step[T](self, operation: Awaitable[T], stage: str) -> T:
        """Await one transport step, racing cancellation and a finite timeout."""
        return await transport_step(
            operation, stage, timeout=self._timeout, cancel_event=self._cancel_event, label=_LABEL
        )

    async def stream(self, message: ProviderInput) -> AsyncIterator[ProviderEvent]:
        """Yield one model response, rolling the local history back on failure."""
        if self._closed:
            raise ProviderError("OpenAI session is closed; open a new session.")
        if self._active:
            raise ProviderError("a turn is already streaming")
        if isinstance(message, UserPrompt) and self._pending:
            raise ProviderError("the model requires results for the pending calls")
        if isinstance(message, ToolResults):
            ids = [result.call_id for result in message.results]
            if not self._pending or len(set(ids)) != len(ids) or set(ids) != set(self._pending):
                raise ProviderError("exactly one result per pending call is required")
        self._active = True
        self._cancel_event = asyncio.Event()
        rollback_length = len(self._messages)
        rollback_pending = dict(self._pending)
        finished = False
        try:
            if isinstance(message, UserPrompt):
                self._messages.append({"role": "user", "content": message.text})
            else:
                for result in message.results:
                    content = result.content
                    if result.is_error:
                        content = "The tool call failed. " + content
                    self._messages.append(
                        {"role": "tool", "tool_call_id": result.call_id, "content": content}
                    )
                self._pending = {}
            async for event in self._completion():
                if isinstance(event, TurnFinished):
                    finished = True
                yield event
        finally:
            self._active = False
            if not finished:
                del self._messages[rollback_length:]
                self._pending = rollback_pending

    async def _completion(self) -> AsyncIterator[ProviderEvent]:
        payload: JsonObject = {"model": self._model, "messages": list(self._messages)}
        payload["stream"] = True
        if self._tools:
            payload["tools"] = list(self._tools)
            payload["tool_choice"] = "auto"
        request = self._client.build_request("POST", "chat/completions", json=payload)
        response = await self._step(self._client.send(request, stream=True), "request")
        try:
            if response.status_code != 200:
                raise await self._completion_error(
                    response.status_code, aiter(response.aiter_bytes())
                )
            text_parts: list[str] = []
            fragments: dict[int, dict[str, str]] = {}
            finish: str | None = None
            lines = aiter(response.aiter_lines())
            while True:
                try:
                    line = await self._step(anext(lines), "stream read")
                except StopAsyncIteration:
                    break
                data = sse_data(line)
                if data is None:
                    continue
                if data == SSE_DONE:
                    break
                choice = self._chunk(data)
                if choice is None:
                    continue
                for delta in self._choice_events(choice, text_parts, fragments):
                    yield delta
                reason = choice.get("finish_reason")
                if isinstance(reason, str):
                    finish = reason
            for event in self._turn_events(finish, "".join(text_parts), fragments):
                yield event
        finally:
            with contextlib.suppress(Exception):
                await response.aclose()

    async def _completion_error(
        self, status_code: int, chunks: AsyncIterator[bytes]
    ) -> ProviderError:
        message = f"OpenAI-compatible endpoint returned HTTP {status_code}."
        if status_code in (401, 403):
            return ProviderError(f"{message} {_KEY_GUIDANCE}")
        if status_code not in (400, 404):
            return ProviderError(message)
        body = bytearray()
        while len(body) < _MAX_ERROR_BODY_BYTES:
            try:
                chunk = await self._step(anext(chunks), "error response read")
            except StopAsyncIteration:
                break
            body.extend(chunk[: _MAX_ERROR_BODY_BYTES - len(body)])
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            data = None
        error = data.get("error") if isinstance(data, dict) else None
        detail = error.get("message") if isinstance(error, dict) else error
        if status_code == 400 and isinstance(detail, str) and "does not support tools" in detail:
            return ProviderError(
                f"{message} The selected model does not support tool calling, which the "
                "agent requires. Select a tool-capable model with --openai-model "
                "(for example llama3.2:latest on Ollama); no text-only fallback was used."
            )
        if status_code == 404:
            if (
                isinstance(detail, str)
                and "model" in detail.lower()
                and "not found" in detail.lower()
            ):
                return ProviderError(
                    f"{message} The selected model was not found. Check the exact "
                    "--openai-model name against the server's installed models "
                    "(ollama list on Ollama) and install it on that server if needed."
                )
            return ProviderError(
                f"{message} Check --openai-url (including /v1) and --openai-model. "
                "Ollama also returns HTTP 404 when the requested model is not installed; "
                "this does not necessarily mean /v1/chat/completions is missing."
            )
        return ProviderError(message)

    @staticmethod
    def _chunk(data: str) -> JsonObject | None:
        try:
            parsed = json.loads(data)
        except ValueError:
            raise ProviderError("the endpoint streamed a malformed completion chunk") from None
        if not isinstance(parsed, dict):
            raise ProviderError("the endpoint streamed a malformed completion chunk")
        choices = parsed.get("choices")
        if not isinstance(choices, list) or not choices:
            return None  # usage-only or keep-alive chunks carry no choice
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ProviderError("the endpoint streamed a malformed completion chunk")
        result: JsonObject = {key: finite_json(value) for key, value in choice.items()}
        return result

    def _choice_events(
        self, choice: JsonObject, text_parts: list[str], fragments: dict[int, dict[str, str]]
    ) -> list[TextDelta]:
        delta = choice.get("delta")
        if delta is None:
            return []
        if not isinstance(delta, dict):
            raise ProviderError("the endpoint streamed a malformed completion delta")
        events: list[TextDelta] = []
        content = delta.get("content")
        if isinstance(content, str) and content:
            text_parts.append(content)
            events.append(TextDelta(content))
        calls = delta.get("tool_calls")
        if calls is None:
            return events
        if not isinstance(calls, list):
            raise ProviderError("the endpoint streamed a malformed tool-call delta")
        for item in calls:
            self._merge_fragment(item, fragments)
        return events

    @staticmethod
    def _merge_fragment(item: JsonValue, fragments: dict[int, dict[str, str]]) -> None:
        if not isinstance(item, dict):
            raise ProviderError("the endpoint streamed a malformed tool-call delta")
        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ProviderError("the endpoint streamed a tool-call delta without an index")
        slot = fragments.setdefault(index, {"id": "", "name": "", "arguments": ""})
        identifier = item.get("id")
        if isinstance(identifier, str) and identifier:
            if slot["id"] and slot["id"] != identifier:
                raise ProviderError("the endpoint changed a tool call ID mid-stream")
            slot["id"] = identifier
        function = item.get("function")
        if function is None:
            return
        if not isinstance(function, dict):
            raise ProviderError("the endpoint streamed a malformed tool-call delta")
        name = function.get("name")
        if isinstance(name, str) and name:
            if slot["name"] and slot["name"] != name:
                raise ProviderError("the endpoint changed a tool name mid-stream")
            slot["name"] = name
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            slot["arguments"] += arguments

    def _turn_events(
        self, finish: str | None, text: str, fragments: dict[int, dict[str, str]]
    ) -> list[ProviderEvent]:
        if finish is None:
            raise ProviderError("the model stream ended without a finish reason")
        if finish not in ("stop", "tool_calls"):
            raise ProviderError("the model stopped before completing its response")
        if not fragments:
            if finish == "tool_calls":
                raise ProviderError("the model finished with tool_calls but proposed none")
            self._messages.append({"role": "assistant", "content": text})
            return [TurnFinished()]
        proposed: dict[str, ToolCall] = {}
        recorded: list[JsonValue] = []
        for index in sorted(fragments):
            slot = fragments[index]
            call_id, name = slot["id"], slot["name"]
            if not call_id.strip() or call_id in self._seen or call_id in proposed:
                raise ProviderError("the model proposed a missing or reused tool call ID")
            if name not in self._names:
                raise ProviderError("the model proposed an unknown tool")
            try:
                arguments = json.loads(slot["arguments"] or "{}")
            except ValueError:
                raise ProviderError("the model proposed malformed tool arguments") from None
            validated = finite_json(arguments)
            if not isinstance(validated, dict):
                raise ProviderError("tool arguments must be a JSON object")
            proposed[call_id] = ToolCall(call_id, name, validated)
            recorded.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(validated, ensure_ascii=True, allow_nan=False),
                    },
                }
            )
        self._messages.append(
            {"role": "assistant", "content": text or None, "tool_calls": recorded}
        )
        self._seen.update(proposed)
        self._pending = proposed
        events: list[ProviderEvent] = [ToolCallProposed(call) for call in proposed.values()]
        events.append(TurnFinished("tool_calls"))
        return events

    async def cancel(self) -> None:
        """Interrupt the active turn; no target operation is started or undone."""
        if self._active:
            self._cancel_event.set()

    async def close(self) -> None:
        """Cancel active streaming and reject subsequent turns idempotently."""
        if self._closed:
            return
        self._closed = True
        await self.cancel()
