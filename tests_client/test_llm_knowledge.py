# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Knowledge tools use fixed, read-only pyGdbKnowledge API routes."""

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

import pytest
import requests

from pyGdbClient.llm.knowledge import KnowledgeClient, KnowledgeError, read_knowledge_token
from pyGdbClient.llm.contracts import (
    ActionEffect,
    ActionPlan,
    ContextFact,
    MemoryMapContext,
    PlannedAction,
    TargetContext,
    ToolCall,
)
from pyGdbClient.llm.gateway import ToolGateway
from pyGdbClient.llm.policy import ProtectionPolicy
from pyGdbClient.llm.tools import ToolCatalogue, ToolValidationError
from pyGdbClient.rpc import JsonRpcClient
from rpc_fakes import FakeRpcServer


class _Response:
    def __init__(self, value: object, status_code: int = 200) -> None:
        self.status_code = status_code
        self.content = json.dumps(value).encode()
        self._value = value

    def json(self) -> object:
        return self._value


def test_knowledge_search_translates_filters_and_sends_only_read_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests_seen: list[dict[str, Any]] = []

    def request(session: requests.Session, method: str, url: str, **kwargs: Any) -> _Response:
        assert not session.trust_env
        requests_seen.append({"method": method, "url": url, **kwargs})
        return _Response({"results": [{"text": "source passage"}]})

    monkeypatch.setattr(requests.Session, "request", request)
    call = ToolCatalogue(include_knowledge=True).prepare(
        ToolCall(
            "search",
            "knowledge_search",
            {
                "query": "UART DMA RX lost bytes",
                "manufacturer": "ST",
                "device": "STM32H743",
                "peripheral": "USART",
                "document_types": ["reference_manual", "errata"],
                "limit": 8,
            },
        )
    )
    client = KnowledgeClient("http://localhost:8081/", "read-token")
    result = asyncio.run(client.execute(call.name, json.loads(call.params_json), call.timeout))
    assert result == {"results": [{"text": "source passage"}]}
    assert requests_seen == [
        {
            "method": "POST",
            "url": "http://localhost:8081/api/v1/search",
            "headers": {
                "Accept": "application/json",
                "Authorization": "Bearer read-token",
                "Content-Type": "application/json",
            },
            "json": {
                "query": "UART DMA RX lost bytes",
                "filters": {
                    "manufacturer": "ST",
                    "device": "STM32H743",
                    "peripheral": "USART",
                    "document_type": ["reference_manual", "errata"],
                },
                "limit": 8,
            },
            "timeout": 30,
            "allow_redirects": False,
        }
    ]


def test_get_document_returns_metadata_and_table_of_contents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests_seen: list[tuple[str, str]] = []
    responses = iter(
        (
            _Response({"document_id": "manual", "status": "active", "version": 2}),
            _Response(
                {
                    "sections": [{"section_id": "usart", "title": "USART"}],
                    "has_more": False,
                }
            ),
        )
    )

    def request(session: requests.Session, method: str, url: str, **kwargs: Any) -> _Response:
        assert not session.trust_env
        requests_seen.append((method, url))
        return next(responses)

    monkeypatch.setattr(requests.Session, "request", request)
    result = asyncio.run(
        KnowledgeClient("http://localhost:8081").execute(
            "knowledge_get_document", {"document_id": "manual"}, 5
        )
    )
    assert result == {
        "document": {"document_id": "manual", "status": "active", "version": 2},
        "table_of_contents": {
            "sections": [{"section_id": "usart", "title": "USART"}],
            "has_more": False,
        },
    }
    assert requests_seen == [
        ("GET", "http://localhost:8081/api/v1/documents/manual"),
        ("GET", "http://localhost:8081/api/v1/documents/manual/sections?limit=100"),
    ]


def test_get_section_uses_encoded_fixed_document_route(monkeypatch: pytest.MonkeyPatch) -> None:
    requests_seen: list[tuple[str, str]] = []

    def request(session: requests.Session, method: str, url: str, **kwargs: Any) -> _Response:
        assert not session.trust_env
        requests_seen.append((method, url))
        return _Response({"document_id": "manual", "section_id": "usart", "text": "source"})

    monkeypatch.setattr(requests.Session, "request", request)
    result = asyncio.run(
        KnowledgeClient("http://localhost:8081").execute(
            "knowledge_get_section",
            {"document_id": "manual", "section_id": "usart"},
            5,
        )
    )
    assert result["text"] == "source"
    assert requests_seen == [("GET", "http://localhost:8081/api/v1/documents/manual/section/usart")]


def test_gateway_dispatches_knowledge_tools_to_http_not_gdb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_urls: list[str] = []

    def request(session: requests.Session, method: str, url: str, **kwargs: Any) -> _Response:
        assert not session.trust_env
        request_urls.append(url)
        return _Response({"results": [{"text": "untrusted manual passage"}]})

    monkeypatch.setattr(requests.Session, "request", request)
    fact = ContextFact("test", "configuration")
    context = TargetContext(
        "context-1",
        fact,
        ContextFact(None, "configuration", "not declared"),
        fact,
        fact,
        fact,
        fact,
        MemoryMapContext((), "toolkit"),
        fact,
    )
    snapshot = json.loads(json.dumps(asdict(context)))

    async def exercise() -> None:
        async with FakeRpcServer({"target.context": snapshot}) as server:
            rpc = JsonRpcClient(server.url)
            await rpc.connect()
            try:
                call = ToolCall("search", "knowledge_search", {"query": "USART"})
                plan = ActionPlan(
                    "plan-1",
                    context.context_id,
                    "Search the reference manual",
                    (PlannedAction(call, "Find USART details", (ActionEffect.UNKNOWN,)),),
                )
                results = await ToolGateway(
                    rpc,
                    ProtectionPolicy(knowledge_enabled=True),
                    knowledge=KnowledgeClient("http://localhost:8081"),
                ).execute(plan, context)
                assert len(results) == 1 and not results[0].is_error
                assert json.loads(results[0].content)["result"]["results"][0]["text"] == (
                    "untrusted manual passage"
                )
                assert server.methods == ["target.context"]
            finally:
                await rpc.close()

    asyncio.run(exercise())
    assert request_urls == ["http://localhost:8081/api/v1/search"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "USART", "document_types": "errata"},
        {"query": "USART", "document_types": ["errata", ["invalid"]]},
        {"query": "USART", "document_types": ["errata", "errata"]},
        {"query": "USART", "limit": 0},
        {"query": "USART", "surprise": True},
    ],
)
def test_knowledge_search_rejects_invalid_tool_arguments(arguments: dict[str, Any]) -> None:
    with pytest.raises(ToolValidationError):
        ToolCatalogue(include_knowledge=True).prepare(
            ToolCall("search", "knowledge_search", arguments)
        )


@pytest.mark.parametrize("url", ["ftp://host", "http://user:pass@host", "http://host/?x=1"])
def test_knowledge_client_rejects_unsafe_base_urls(url: str) -> None:
    with pytest.raises(ValueError, match="knowledge URL"):
        KnowledgeClient(url)


def test_document_ids_are_rejected_before_path_interpolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_request(*args: object, **kwargs: object) -> _Response:
        pytest.fail("invalid document IDs must not trigger an HTTP request")

    monkeypatch.setattr(requests.Session, "request", unexpected_request)
    with pytest.raises(KnowledgeError, match="invalid document_id"):
        asyncio.run(
            KnowledgeClient("http://localhost:8081").execute(
                "knowledge_get_document", {"document_id": "../secret"}, 5
            )
        )


def test_read_token_file_is_trimmed_and_never_part_of_client_repr(tmp_path: Path) -> None:
    token_file = tmp_path / "read-token"
    token_file.write_text("  test-read-token \n", encoding="utf-8")
    token = read_knowledge_token(token_file)
    client = KnowledgeClient("http://localhost:8081", token)
    assert token == "test-read-token"
    assert token not in repr(client)


def test_read_token_file_rejects_multiple_lines(tmp_path: Path) -> None:
    token_file = tmp_path / "read-token"
    token_file.write_text("first-token\nsecond-token\n", encoding="utf-8")
    with pytest.raises(KnowledgeError, match="one nonempty line"):
        read_knowledge_token(token_file)


def test_knowledge_http_failures_do_not_forward_response_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def request(*args: object, **kwargs: object) -> _Response:
        return _Response({"detail": "sensitive server detail"}, status_code=401)

    monkeypatch.setattr(requests.Session, "request", request)
    with pytest.raises(KnowledgeError, match="HTTP 401") as error:
        asyncio.run(
            KnowledgeClient("http://localhost:8081", "secret").execute(
                "knowledge_search", {"query": "USART"}, 5
            )
        )
    assert "sensitive server detail" not in str(error.value)
