# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Bounded local source access and the OpenCode-to-gateway integration."""

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from pyGdbClient.audit import AuditContextManager
from pyGdbClient.llm import sources
from pyGdbClient.llm.approval import ApprovalRequest, ApprovalResponse
from pyGdbClient.llm.contracts import ActionPlan, PlannedAction, PolicyOutcome, ToolCall
from pyGdbClient.llm.controller import AgentController, AgentSettings
from pyGdbClient.llm.gateway import ToolGateway
from pyGdbClient.llm.policy import ProtectionPolicy
from pyGdbClient.llm.sources import MAX_FILE_BYTES, SourceBrowser, SourceError
from pyGdbClient.llm.tools import ToolCatalogue, ToolValidationError
from pyGdbClient.rpc import JsonRpcClient
from rpc_fakes import FakeRpcServer
from test_audit import _loaded, _responses
from test_llm_contracts import _context
from test_llm_opencode import Server, answer, provider


def _call(browser: SourceBrowser, name: str, **arguments: Any) -> dict[str, Any]:
    call = ToolCatalogue().prepare(ToolCall("id", name, arguments))
    return browser.execute(name, json.loads(call.params_json))


def test_read_glob_grep_and_bounded_results(tmp_path: Path) -> None:
    source = tmp_path / "output/src/app"
    source.mkdir(parents=True)
    (source / "main.c").write_text("one\nneedle here\nneedle again\n", encoding="utf-8")
    (source / "other.h").write_text("needle header\n", encoding="utf-8")
    browser = SourceBrowser(tmp_path)
    result = _call(browser, "read", path="output/src/app/main.c", offset=2, limit=1)
    assert result["content"] == "2: needle here"
    assert result["truncated"] is True
    result = _call(browser, "glob", path="output/src", pattern="**/*.c")
    assert result["matches"] == [{"path": "output/src/app/main.c"}]
    result = _call(browser, "grep", path="output/src", pattern="needle", glob="**/*.c")
    assert result["matches"] == [
        {"path": "output/src/app/main.c", "line": 2, "text": "needle here"},
        {"path": "output/src/app/main.c", "line": 3, "text": "needle again"},
    ]


@pytest.mark.parametrize("path", ["/etc/passwd", "../file", "dir/../file", ".git/config", "a//b"])
def test_rejects_paths_outside_project_or_hidden(path: str, tmp_path: Path) -> None:
    with pytest.raises(ToolValidationError):
        _call(SourceBrowser(tmp_path), "read", path=path)


def test_rejects_links_special_files_binary_and_large_files(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="utf-8")
    (root / "link").symlink_to(outside)
    (root / "dirlink").symlink_to(tmp_path, target_is_directory=True)
    os.mkfifo(root / "fifo")
    (root / "binary").write_bytes(b"binary\0data")
    (root / "large").write_bytes(b"a" * (MAX_FILE_BYTES + 1))
    browser = SourceBrowser(root)
    for path in ("link", "dirlink/file", "fifo", "binary", "large"):
        with pytest.raises(SourceError):
            _call(browser, "read", path=path)
    paths = _call(browser, "glob", path=".", pattern="**")["matches"]
    assert sorted(item["path"] for item in paths) == ["binary", "large"]


def test_root_replacement_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    browser = SourceBrowser(root)
    root.rename(tmp_path / "previous")
    root.mkdir()
    with pytest.raises(SourceError, match="project directory changed"):
        _call(browser, "glob", path=".", pattern="**")


def test_grep_reports_skipped_files_and_scan_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "binary.c").write_bytes(b"needle\0binary")
    (tmp_path / "large.c").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    (tmp_path / "main.c").write_text("needle\nneedle again\n", encoding="utf-8")
    browser = SourceBrowser(tmp_path)
    result = _call(browser, "grep", path=".", pattern="needle", glob="*.c")
    assert result["skipped_non_text_or_large"] == 2
    assert len(result["matches"]) == 2
    assert result["truncated"] is False
    monkeypatch.setattr(sources, "MAX_RESULTS", 1)
    result = _call(browser, "grep", path=".", pattern="needle", glob="*.c")
    assert len(result["matches"]) == 1
    assert result["truncated"] is True
    monkeypatch.setattr(sources, "MAX_SCAN_ENTRIES", 0)
    assert _call(browser, "glob", path=".", pattern="**")["truncated"] is True


def test_source_tools_are_advertised_only_when_requested() -> None:
    catalogue = ToolCatalogue()
    normal = {definition.name for definition in catalogue.definitions()}
    with_sources = {definition.name for definition in catalogue.definitions(include_sources=True)}
    assert with_sources - normal == {"read", "glob", "grep"}


def test_source_policy_still_applies_denials_and_mixed_plan_approvals() -> None:
    source = ToolCall("source", "read", {"path": "main.c"})
    target = ToolCall("target", "memory_read", {"address": 0, "length": 1})
    context = _context()
    source_plan = ActionPlan(
        "p", context.context_id, "Inspect source", (PlannedAction(source, "Read"),)
    )
    assert ProtectionPolicy().evaluate(source_plan, context).outcome is PolicyOutcome.ALLOW
    assert (
        ProtectionPolicy(denied_tools=("read",)).evaluate(source_plan, context).outcome
        is PolicyOutcome.DENY
    )
    mixed = ActionPlan(
        "mixed",
        context.context_id,
        "Inspect source and target",
        (PlannedAction(source, "Read source"), PlannedAction(target, "Read target")),
    )
    assert ProtectionPolicy().evaluate(mixed, context).outcome is PolicyOutcome.REQUIRE_APPROVAL


def test_source_timeout_reports_failure_without_disclosing_late_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = Event()
    browser = SourceBrowser(tmp_path)

    def blocked_read(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        release.wait(5)
        return {"content": "late result"}

    monkeypatch.setattr(browser, "execute", blocked_read)

    class Requester:
        async def request(self, method: str, params: Any = None, timeout: float = 60) -> Any:
            assert method == "target.context"
            return json.loads(json.dumps(asdict(_context())))

    async def run() -> None:
        context = _context()
        call = ToolCall("read", "read", {"path": "main.c", "timeout": 0.01})
        plan = ActionPlan("p", context.context_id, "Read", (PlannedAction(call, "Read"),))
        gateway = ToolGateway(
            Requester(), ProtectionPolicy(), sources=browser, sources_current=lambda: True
        )
        try:
            (result,) = await gateway.execute(plan, context)
            assert result.is_error
            assert "timed out" in result.content
            assert "late result" not in result.content
        finally:
            release.set()

    asyncio.run(run())


def test_opencode_uses_local_sources_through_policy_and_does_not_send_rpc(
    tmp_path: Path,
) -> None:
    (tmp_path / "main.c").write_text("int answer = 42;\n", encoding="utf-8")

    def proposal(call_id: str, name: str, arguments: dict[str, object]) -> str:
        return json.dumps(
            {"type": "tool_calls", "calls": [{"id": call_id, "name": name, "arguments": arguments}]}
        )

    async def run() -> None:
        server = Server(
            proposal("glob1", "glob", {"path": ".", "pattern": "**/*.c"}),
            proposal("grep1", "grep", {"path": ".", "pattern": "answer", "glob": "**/*.c"}),
            proposal("read1", "read", {"path": "main.c"}),
            answer("The value is 42."),
        )
        backend = provider(server)
        manager = AuditContextManager()
        manager.set_project(_loaded(tmp_path))
        responses = _responses() | {"target.context": asdict(_context())}
        approvals: list[ApprovalRequest] = []

        async def decide(request: ApprovalRequest) -> ApprovalResponse:
            approvals.append(request)
            return request.respond(approved=False)

        async with FakeRpcServer(responses) as target:
            client = JsonRpcClient(target.url)
            await client.connect()
            controller = AgentController(
                AgentSettings(backend),
                client,
                approver=decide,
                sink=lambda event: None,
                audit=manager,
            )
            result = await controller.submit("Read the project sources")
            assert result.reason == "completed"
            assert result.tool_calls == 3
            assert approvals == []
            assert '"name": "read"' in server.prompts[0]
            assert "int answer = 42;" in server.prompts[-1]
            assert not any(method.startswith("local.source") for method in target.methods)
            manager.clear_project()
            server.answers.extend(
                (proposal("read2", "read", {"path": "main.c"}), answer("Denied."))
            )
            result = await controller.submit("Read again")
            assert result.reason == "completed"
            assert "loaded project changed" in server.prompts[-1]
            await controller.close()
            await client.close()

    asyncio.run(run())
