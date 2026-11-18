# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""The audit context aggregates the loaded project and pyGdbServer session state."""

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from pyGdbClient.app import PyGdbClientApp
from pyGdbClient.audit import (
    MAX_SVD_PERIPHERALS,
    AuditContext,
    AuditContextManager,
    AuditObservation,
    ProjectContext,
)
from pyGdbClient.llm.rendering import describe_audit
from pyGdbClient.project import LoadedProject, ProjectArtifact
from pyGdbClient.rpc import RpcError
from pyGdbToolkit.shared.rtos import DEFAULT_PROJECT_REGISTRY

NOW = "2026-01-01T00:00:00+00:00"
PROJECT = b"""
name = 'demo'
version = 'v1'
[kernel]
scm.git.uri = 'https://github.com/camelot-os/sentry-kernel.git'
scm.git.revision = 'v0.4.8'
[runtime]
[application.hello]
provides = ['hello.elf']
"""
RTOS = {"schema_version": 1, "selected_rtos": "camelot", "loaded": True, "task_list": None}
SVD = {
    "loaded": True,
    "device": "STM32U5A5",
    "peripherals": [
        {"name": "GPIOA", "description": "port", "base_address": 0x42020000, "registers": [{}]},
        {"name": "RCC", "base_address": 0x46020C00, "registers": [{}, {}]},
    ],
}
SERVER = {"ready": True, "ocd": {"executable": "openocd"}, "gdb": {"interpreter": "mi3"}}


def _responses() -> dict[str, Any]:
    return {
        "rtos.status": RTOS,
        "target.status": {"state": "stopped", "core": 0},
        "target.cores": {"cores": [{"id": 0}, {"id": 1}]},
        "svd.peripherals": SVD,
        "server.status": SERVER,
    }


class FakeRpc:
    """Answer audit status requests from a scripted table, recording them."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict[str, Any] | None, float]] = []

    async def request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 60.0
    ) -> Any:
        """Return the scripted result or raise the scripted exception."""
        self.requests.append((method, params, timeout))
        result = self.responses[method]
        if isinstance(result, BaseException):
            raise result
        return result


def _loaded(root: Path) -> LoadedProject:
    return LoadedProject(
        "camelot",
        root,
        "/srv/stage/demo",
        (ProjectArtifact("project.toml", len(PROJECT), "ab" * 32),),
        DEFAULT_PROJECT_REGISTRY.describe("camelot", PROJECT),
        {"loaded": True},
    )


def _refresh(manager: AuditContextManager, responses: dict[str, Any]) -> AuditContext:
    return asyncio.run(manager.refresh(FakeRpc(responses)))


def test_refresh_gathers_every_part_with_its_provenance(tmp_path: Path) -> None:
    """Available parts carry their data and the server method they came from."""
    (tmp_path / "output/src/kernel").mkdir(parents=True)
    manager = AuditContextManager(timeout=5, clock=lambda: NOW)
    project = manager.set_project(_loaded(tmp_path))
    rpc = FakeRpc(_responses())

    audit = asyncio.run(manager.refresh(rpc))

    assert manager.latest is audit and manager.project is project
    assert audit.unavailable == ()
    assert {method for method, _, _ in rpc.requests} == set(_responses())
    assert all(timeout == 10 for _, _, timeout in rpc.requests)
    assert dict((method, params) for method, params, _ in rpc.requests)["rtos.status"] == {
        "timeout": 5
    }
    assert audit.rtos == AuditObservation("pyGdbServer rtos.status", RTOS)
    assert audit.svd.data == {
        "device": "STM32U5A5",
        "peripheral_count": 2,
        "truncated": False,
        "peripherals": [
            {"name": "GPIOA", "base_address": 0x42020000, "registers": 1},
            {"name": "RCC", "base_address": 0x46020C00, "registers": 2},
        ],
    }
    document = json.loads(json.dumps(audit.to_json(), allow_nan=False))
    assert document["schema_version"] == 1 and document["captured_at"] == NOW
    assert document["server"] == {"source": "pyGdbServer server.status", "data": SERVER}
    assert document["project"]["local_root"] == str(tmp_path)
    assert document["project"]["name"] == "demo"
    assert document["project"]["artifacts"] == [
        {"path": "project.toml", "size": len(PROJECT), "sha256": "ab" * 32}
    ]
    kernel, runtime, hello = document["project"]["components"]
    assert kernel["sources"] == "output/src/kernel"
    assert runtime["sources"] is None and hello["sources"] is None
    assert hello["executables"] == ["output/build/camelot_private/hello.elf"]


@pytest.mark.parametrize(
    ("part", "method", "failure", "reason"),
    [
        ("rtos", "rtos.status", RpcError(-32601, "Method not found"), "Method not found"),
        ("execution", "target.status", ConnectionError("Not connected"), "Not connected"),
        ("cores", "target.cores", TimeoutError(), "TimeoutError"),
        ("rtos", "rtos.status", {**RTOS, "schema_version": 2}, "unsupported schema"),
        ("server", "server.status", ["ready"], "did not return an object"),
        ("server", "server.status", {"ratio": float("nan")}, "not finite JSON"),
        ("svd", "svd.peripherals", {"loaded": False}, "use svd load"),
        ("svd", "svd.peripherals", {"loaded": True}, "no peripheral list"),
        ("svd", "svd.peripherals", {"loaded": True, "peripherals": [1]}, "malformed"),
    ],
)
def test_unobtainable_parts_are_explicitly_unavailable(
    part: str, method: str, failure: Any, reason: str
) -> None:
    """One failing source never hides the others nor becomes a plausible default."""
    responses = _responses()
    responses[method] = failure

    audit = _refresh(AuditContextManager(clock=lambda: NOW), responses)

    assert audit.unavailable == ("project", part)
    observation = getattr(audit, part)
    assert observation.data is None and reason in observation.unavailable_reason
    assert audit.to_json()[part] == {
        "source": f"pyGdbServer {method}",
        "unavailable_reason": observation.unavailable_reason,
    }
    assert audit.to_json()["project"] == {
        "unavailable_reason": "no project is loaded; use the project command"
    }


def test_svd_summary_is_bounded() -> None:
    """Large SVD devices are summarized up to a fixed number of peripherals."""
    responses = _responses()
    responses["svd.peripherals"] = {
        "loaded": True,
        "device": "big",
        "peripherals": [
            {"name": f"P{index}", "base_address": index, "registers": None}
            for index in range(MAX_SVD_PERIPHERALS + 1)
        ],
    }

    svd = _refresh(AuditContextManager(clock=lambda: NOW), responses).svd.data

    assert svd is not None
    assert svd["peripheral_count"] == MAX_SVD_PERIPHERALS + 1 and svd["truncated"] is True
    assert isinstance(svd["peripherals"], list)
    assert len(svd["peripherals"]) == MAX_SVD_PERIPHERALS
    assert svd["peripherals"][0] == {"name": "P0", "base_address": 0, "registers": 0}


def test_project_can_be_replaced_and_cleared(tmp_path: Path) -> None:
    """The latest project replaces the previous one; clearing forgets it."""
    manager = AuditContextManager(clock=lambda: NOW)
    first = manager.set_project(_loaded(tmp_path / "a"))
    second = manager.set_project(_loaded(tmp_path / "b"))
    assert manager.project is second and second != first
    assert second == ProjectContext.from_loaded(_loaded(tmp_path / "b"), NOW)
    manager.clear_project()
    assert manager.project is None


@pytest.mark.parametrize(
    ("source", "data", "reason"),
    [(" ", {}, None), ("rpc", None, None), ("rpc", {}, "both"), ("rpc", None, " ")],
)
def test_observations_require_provenance_and_one_outcome(
    source: str, data: dict[str, Any] | None, reason: str | None
) -> None:
    """An observation is either data or an explained absence, from a named source."""
    with pytest.raises(ValueError):
        AuditObservation(source, data, reason)


def test_unavailable_reasons_are_bounded() -> None:
    """Server error texts cannot flood the disclosed context."""
    observation = AuditObservation.unavailable("rpc", "x" * 1000)
    assert observation.unavailable_reason == "x" * 240


@pytest.mark.parametrize("timeout", [0, -1, 301, float("nan"), float("inf"), True])
def test_manager_rejects_unbounded_timeouts(timeout: float) -> None:
    """Every audit request is bounded in time."""
    with pytest.raises(ValueError, match="timeout"):
        AuditContextManager(timeout=timeout)


def test_audit_is_summarized_for_the_auditor(tmp_path: Path) -> None:
    """The dashboard summary names the project, its components and every part's state."""
    manager = AuditContextManager(clock=lambda: NOW)
    manager.set_project(_loaded(tmp_path))
    responses = _responses()
    responses["svd.peripherals"] = {"loaded": False}

    lines = [line.plain for line in describe_audit(_refresh(manager, responses))]

    assert lines[0] == f"Audit context captured {NOW}"
    assert f"  Project: camelot demo v1 · {tmp_path}" in lines
    assert any(
        line.startswith("    kernel (kernel) git v0.4.8 · sources unavailable locally")
        for line in lines
    )
    assert "  RTOS: camelot loaded · 0 task slot(s) (pyGdbServer rtos.status)" in lines
    assert "  Execution: stopped · core 0 (pyGdbServer target.status)" in lines
    assert "  Cores: 2 core(s) (pyGdbServer target.cores)" in lines
    assert any(line.startswith("  SVD: unavailable: no SVD device is loaded") for line in lines)
    assert "  Server: OCD openocd · GDB mi3 (pyGdbServer server.status)" in lines


def test_dashboard_audit_command_displays_and_saves_the_context(tmp_path: Path) -> None:
    """``audit FILE`` refreshes from the server, summarizes, then writes local JSON."""
    app = PyGdbClientApp("ws://127.0.0.1:1234")
    app._connected = True
    app._audit = AuditContextManager(clock=lambda: NOW)
    rpc = FakeRpc(_responses())
    app.client.request = rpc.request  # type: ignore[method-assign]
    app._append_output = Mock()
    app._write_output = Mock()
    target = tmp_path / "audit file.json"

    asyncio.run(app.execute_command(f'audit "{target}"'))

    assert {method for method, _, _ in rpc.requests} == set(_responses())
    (lines,), _ = app._write_output.call_args
    assert lines[0].plain == f"Audit context captured {NOW}"
    assert json.loads(target.read_text("utf-8")) == app._audit.latest.to_json()
    messages = [call.args[0] for call in app._append_output.call_args_list]
    assert f"Audit context saved to {target}" in messages


def test_dashboard_audit_command_reports_usage_errors() -> None:
    """Extra arguments are rejected without contacting the server."""
    app = PyGdbClientApp("ws://127.0.0.1:1234")
    app._connected = True
    rpc = FakeRpc(_responses())
    app.client.request = rpc.request  # type: ignore[method-assign]
    app._append_output = Mock()

    asyncio.run(app.execute_command("audit a b"))

    assert rpc.requests == []
    app._append_output.assert_called_with("Usage: audit [FILE]", error=True)
