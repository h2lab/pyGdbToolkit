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
                "device_family": "STM32H7",
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
                    "device_family": "STM32H7",
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
                    "offset": 0,
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
            "offset": 0,
            "has_more": False,
        },
    }
    assert requests_seen == [
        ("GET", "http://localhost:8081/api/v1/documents/manual"),
        ("GET", "http://localhost:8081/api/v1/documents/manual/sections?limit=100&offset=0"),
    ]


def test_document_pagination_is_forwarded_to_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    urls: list[str] = []

    def request(session: requests.Session, method: str, url: str, **kwargs: Any) -> _Response:
        urls.append(url)
        if "/sections?" in url:
            return _Response(
                {"sections": [{"section_id": "s0100"}], "offset": 100, "has_more": False}
            )
        return _Response({"document_id": "manual"})

    monkeypatch.setattr(requests.Session, "request", request)
    call = ToolCatalogue(include_knowledge=True).prepare(
        ToolCall(
            "page", "knowledge_get_document", {"document_id": "manual", "offset": 100, "limit": 20}
        )
    )
    result = asyncio.run(
        KnowledgeClient("http://localhost:8081").execute(
            call.name, json.loads(call.params_json), call.timeout
        )
    )
    assert result["table_of_contents"]["sections"][0]["section_id"] == "s0100"
    assert urls[-1].endswith("/documents/manual/sections?limit=20&offset=100")


@pytest.mark.parametrize("pagination", [{"offset": -1}, {"offset": True}, {"limit": 101}])
def test_invalid_document_pagination_is_rejected_before_http(
    monkeypatch: pytest.MonkeyPatch, pagination: dict[str, Any]
) -> None:
    def unexpected_request(*args: object, **kwargs: object) -> _Response:
        pytest.fail("invalid pagination must not trigger an HTTP request")

    monkeypatch.setattr(requests.Session, "request", unexpected_request)
    with pytest.raises(KnowledgeError, match="pagination"):
        asyncio.run(
            KnowledgeClient("http://localhost:8081").execute(
                "knowledge_get_document", {"document_id": "manual", **pagination}, 5
            )
        )


def test_large_search_keeps_ranked_results_and_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    passages = [
        {
            "chunk_id": f"chunk-{index}",
            "document_id": "manual",
            "page": index + 1,
            "score": 1.0,
            "url": "https://example.test/manual",
            "truncated": False,
            "text": "é" * 1500,
        }
        for index in range(10)
    ]
    monkeypatch.setattr(
        requests.Session, "request", lambda *args, **kwargs: _Response({"results": passages})
    )
    result = asyncio.run(
        KnowledgeClient("http://localhost:8081").execute("knowledge_search", {"query": "USART"}, 5)
    )
    results = result["results"]
    assert 0 < len(results) < len(passages)
    assert results == passages[: len(results)]
    assert result["client_truncated"] is True
    assert result["client_omitted_items"] == len(passages) - len(results)
    content = json.dumps({"result": result, "truncated": False}, ensure_ascii=True)
    assert len(content.encode()) <= 24 * 1024


@pytest.mark.parametrize("tool", ["knowledge_search", "knowledge_get_section"])
def test_large_single_passage_keeps_structured_metadata_and_marks_text_truncation(
    monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    passage = {
        "document_id": "manual",
        "section_id": "s0001",
        "page": 42,
        "text": "Ω" * 10000,
        "truncated": False,
    }
    response = {"results": [passage]} if tool == "knowledge_search" else passage
    monkeypatch.setattr(requests.Session, "request", lambda *args, **kwargs: _Response(response))
    args = (
        {"query": "USART"}
        if tool == "knowledge_search"
        else {"document_id": "manual", "section_id": "s0001"}
    )
    result = asyncio.run(KnowledgeClient("http://localhost:8081").execute(tool, args, 5))
    kept = result["results"][0] if tool == "knowledge_search" else result
    assert kept["document_id"] == "manual" and kept["page"] == 42
    assert kept["truncated"] is False
    assert kept["client_text_truncated"] is True
    assert kept["client_original_text_characters"] == 10000
    assert 0 < len(kept["text"]) < 10000
    assert len(json.dumps({"result": result, "truncated": False}, ensure_ascii=True)) <= 24 * 1024


def test_large_table_of_contents_can_continue_after_bounded_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sections = [{"section_id": f"s{index:04}", "title": "x" * 1000} for index in range(100)]

    def request(session: requests.Session, method: str, url: str, **kwargs: Any) -> _Response:
        if "/sections?" in url:
            return _Response({"sections": sections, "offset": 100, "has_more": False})
        return _Response({"document_id": "manual"})

    monkeypatch.setattr(requests.Session, "request", request)
    result = asyncio.run(
        KnowledgeClient("http://localhost:8081").execute(
            "knowledge_get_document", {"document_id": "manual", "offset": 100}, 5
        )
    )
    page = result["table_of_contents"]
    assert 0 < len(page["sections"]) < 100
    assert page["offset"] == 100 and page["has_more"] is True
    assert page["client_truncated"] is True
    assert page["client_omitted_items"] == 100 - len(page["sections"])
    assert len(json.dumps({"result": result, "truncated": False}, ensure_ascii=True)) <= 24 * 1024


def test_oversized_metadata_fails_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        requests.Session,
        "request",
        lambda *args, **kwargs: _Response(
            {"results": [{"document_id": "x" * 30000, "text": "passage"}]}
        ),
    )
    with pytest.raises(KnowledgeError, match="metadata exceeds"):
        asyncio.run(
            KnowledgeClient("http://localhost:8081").execute(
                "knowledge_search", {"query": "USART"}, 5
            )
        )


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
