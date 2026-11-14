# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""The client stages local RTOS projects to pyGdbServer and loads them into GDB."""

import asyncio
import base64
import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from pyGdbClient.app import PyGdbClientApp
from pyGdbClient.project import ProjectLoadError, load_project, supported_project_types
from pyGdbClient.rpc import RpcError
from pyGdbToolkit.shared.rtos import DEFAULT_PROJECT_REGISTRY, project_manifest

PROJECT = (
    b"[kernel]\n[runtime]\n"
    b"[application.hello]\nprovides = ['hello.elf']\n"
    b"[application.sample_c_app]\nprovides = ['sample-c-app.elf']\n"
)
KCONFIG = "output/build/kernel/subprojects/kconfig-1.2/generated_kconfig.json"


def _snapshot(path: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "selected_rtos": "camelot",
        "loaded": True,
        "project_path": path,
        "task_list": {
            "address": 0x2000_0000,
            "size": 0x400,
            "entry_size": 0x200,
            "slots": [
                {
                    "index": 0,
                    "address": 0x2000_0000,
                    "task_name": "blinky",
                    "text_address": 0x0800_4000,
                    "ram_address": 0x2000_4000,
                    "metadata": {},
                },
                {
                    "index": 1,
                    "address": 0x2000_0200,
                    "task_name": None,
                    "text_address": 0,
                    "ram_address": 0,
                    "metadata": {},
                },
            ],
        },
        "scheduler_trace": None,
    }


class FakeProjectRpc:
    """Answer the ``rtos.project.*`` methods with the real shared camelot manifest."""

    def __init__(self, *, chunk_size: int = 7, fail: dict[str, Exception] | None = None) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.files: dict[str, bytearray] = {}
        self.manifest = project_manifest("camelot")
        self.aborted = False
        self.chunk_size = chunk_size
        self.fail = dict(fail or {})
        self.server_path = "/srv/stage/project"
        self.commit_result: dict[str, Any] | Any = None

    @property
    def methods(self) -> list[str]:
        """Received method names, in arrival order."""
        return [method for method, _ in self.requests]

    async def request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 60.0
    ) -> Any:
        """Dispatch one scripted JSON-RPC request."""
        params = dict(params or {})
        self.requests.append((method, params))
        if method in self.fail:
            raise self.fail[method]
        if method == "rtos.project.begin":
            return {
                "name": params["name"],
                "rtos": params["rtos"],
                "manifest": self.manifest.to_dict(),
                "limits": {"chunk_size": self.chunk_size},
            }
        if method == "rtos.project.put":
            path = params["path"]
            data = base64.b64decode(params["content"], validate=True)
            assert len(data) <= self.chunk_size
            buffer = self.files.setdefault(path, bytearray())
            assert params["offset"] == len(buffer)
            buffer.extend(data)
            complete = len(buffer) == params["size"]
            if complete and path == self.manifest.project_file:
                self.manifest = project_manifest("camelot", bytes(buffer))
            return {
                "path": path,
                "next_offset": len(buffer),
                "complete": complete,
                "manifest": self.manifest.to_dict(),
            }
        if method == "rtos.project.commit":
            if self.commit_result is not None:
                return self.commit_result
            return _snapshot(self.server_path)
        if method == "rtos.project.abort":
            self.aborted = True
            return {"name": params["name"], "aborted": True}
        raise AssertionError(f"unexpected method: {method}")


def _make_project(
    root: Path,
    *,
    project: bytes = PROJECT,
    skip: tuple[str, ...] = (),
    kconfigs: tuple[str, ...] = (KCONFIG,),
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.toml").write_bytes(project)
    manifest = project_manifest("camelot", project)
    for path in manifest.required:
        if path in skip or path == "project.toml":
            continue
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(b"artifact:" + path.encode())
    for kconfig in kconfigs:
        (root / kconfig).parent.mkdir(parents=True, exist_ok=True)
        (root / kconfig).write_bytes(b"{}")


def test_supported_project_types_follow_the_shared_registry() -> None:
    """The client advertises exactly the project types the rtos command supports."""
    assert supported_project_types() == ("camelot",)


def test_load_project_streams_manifest_files_and_commits(tmp_path: Path) -> None:
    """The declaration is sent first, then every manifest file, chunked, then commit."""
    root = tmp_path / "blinky project"
    _make_project(root)
    rpc = FakeProjectRpc(chunk_size=7)
    lines: list[str] = []

    result = asyncio.run(load_project(rpc, root, "camelot", progress=lines.append))

    assert rpc.methods[0] == "rtos.project.begin"
    assert rpc.methods[-1] == "rtos.project.commit"
    assert "rtos.project.abort" not in rpc.methods
    put_paths = [params["path"] for method, params in rpc.requests if method.endswith(".put")]
    assert put_paths[0] == "project.toml"
    expected = project_manifest("camelot", PROJECT).required + (KCONFIG,)
    assert set(put_paths) == set(expected)
    assert bytes(rpc.files["project.toml"]) == PROJECT
    assert len(rpc.files["project.toml"]) > rpc.chunk_size  # chunked in several puts
    for path in expected[1:]:
        assert bytes(rpc.files[path]) == (root / path).read_bytes()
    assert result.rtos == "camelot"
    assert result.local_root == root.resolve()
    assert result.server_path == rpc.server_path
    assert set(result.files) == set(expected)
    assert result.task_names == ("blinky",)
    assert lines[0] == "Uploading project.toml"
    assert len(lines) == len(expected)


def test_load_project_records_identity_and_digests_of_what_was_sent(tmp_path: Path) -> None:
    """Every transferred file is identified by the SHA-256 of the bytes the server received."""
    root = tmp_path / "project"
    _make_project(root)
    rpc = FakeProjectRpc(chunk_size=5)

    result = asyncio.run(load_project(rpc, root, "camelot"))

    assert result.files[0] == "project.toml"
    for artifact in result.artifacts:
        sent = bytes(rpc.files[artifact.path])
        assert artifact.size == len(sent)
        assert artifact.sha256 == hashlib.sha256(sent).hexdigest()
    assert result.description == DEFAULT_PROJECT_REGISTRY.describe("camelot", PROJECT)


@pytest.mark.parametrize(
    "project", [b"[kernel\n", b"[kernel]\nscm.git.uri = 1\n", b"[application.a]\n"]
)
def test_load_project_rejects_an_invalid_declaration_before_any_rpc(
    tmp_path: Path, project: bytes
) -> None:
    """A malformed project.toml fails locally, without staging anything."""
    (tmp_path / "project.toml").write_bytes(project)
    rpc = FakeProjectRpc()
    with pytest.raises(ProjectLoadError, match="invalid project.toml"):
        asyncio.run(load_project(rpc, tmp_path, "camelot"))
    assert rpc.requests == []


def test_load_project_rejects_a_declaration_changed_while_uploading(tmp_path: Path) -> None:
    """The described identity must be the declaration the server received."""
    root = tmp_path / "project"
    _make_project(root)
    rpc = FakeProjectRpc(chunk_size=len(PROJECT) + 64)
    request = rpc.request

    async def edit_then_request(
        method: str, params: dict[str, Any] | None = None, timeout: float = 60.0
    ) -> Any:
        if method == "rtos.project.begin":
            (root / "project.toml").write_bytes(PROJECT.replace(b"hello", b"hallo"))
        return await request(method, params, timeout)

    rpc.request = edit_then_request  # type: ignore[method-assign]
    with pytest.raises(ProjectLoadError, match="changed while uploading: project.toml"):
        asyncio.run(load_project(rpc, root, "camelot"))
    assert rpc.aborted


def test_load_project_transfers_empty_required_files(tmp_path: Path) -> None:
    """A zero-byte artifact completes with a single empty chunk."""
    root = tmp_path / "project"
    _make_project(root)
    header = "output/build/kernel/kernel/include/sentry/managers/task_metadata.h"
    (root / header).write_bytes(b"")
    rpc = FakeProjectRpc()

    result = asyncio.run(load_project(rpc, root, "camelot"))

    assert bytes(rpc.files[header]) == b""
    assert header in result.files


def test_load_project_rejects_unsupported_types_before_any_rpc(tmp_path: Path) -> None:
    """Unknown project types fail locally, naming the supported ones."""
    rpc = FakeProjectRpc()
    with pytest.raises(ProjectLoadError, match="supported: camelot"):
        asyncio.run(load_project(rpc, tmp_path, "freertos"))
    assert rpc.requests == []


def test_load_project_requires_the_local_declaration(tmp_path: Path) -> None:
    """A missing root or declaration file fails before contacting the server."""
    rpc = FakeProjectRpc()
    with pytest.raises(ProjectLoadError, match="not a directory"):
        asyncio.run(load_project(rpc, tmp_path / "absent", "camelot"))
    with pytest.raises(ProjectLoadError, match="project.toml"):
        asyncio.run(load_project(rpc, tmp_path, "camelot"))
    assert rpc.requests == []


def test_load_project_reports_missing_required_files_and_aborts(tmp_path: Path) -> None:
    """Every missing manifest file is reported at once and the transfer is aborted."""
    root = tmp_path / "project"
    _make_project(
        root,
        skip=(
            "output/build/camelot_private/layout.json",
            "output/build/camelot_private/sample-c-app.elf",
        ),
    )
    rpc = FakeProjectRpc()
    with pytest.raises(ProjectLoadError) as error:
        asyncio.run(load_project(rpc, root, "camelot"))
    assert "layout.json" in str(error.value) and "sample-c-app.elf" in str(error.value)
    assert rpc.aborted


@pytest.mark.parametrize(
    "kconfigs",
    [
        (),
        (KCONFIG, "output/build/kernel/subprojects/kconfig-2.0/generated_kconfig.json"),
    ],
)
def test_load_project_requires_exactly_one_kconfig(
    tmp_path: Path, kconfigs: tuple[str, ...]
) -> None:
    """Zero or several generated Kconfig files fail explicitly and abort the transfer."""
    root = tmp_path / "project"
    _make_project(root, kconfigs=kconfigs)
    rpc = FakeProjectRpc()
    with pytest.raises(ProjectLoadError, match="exactly one file"):
        asyncio.run(load_project(rpc, root, "camelot"))
    assert rpc.aborted


def test_load_project_aborts_when_the_server_rejects_a_chunk(tmp_path: Path) -> None:
    """A server-side transfer rejection propagates after aborting the staging."""
    root = tmp_path / "project"
    _make_project(root)
    rpc = FakeProjectRpc(fail={"rtos.project.put": RpcError(-32000, "staging full")})
    with pytest.raises(RpcError, match="staging full"):
        asyncio.run(load_project(rpc, root, "camelot"))
    assert rpc.aborted


def test_load_project_never_aborts_once_commit_started(tmp_path: Path) -> None:
    """Failed commits stay frozen on the server because GDB may reference their files."""
    root = tmp_path / "project"
    _make_project(root)
    rpc = FakeProjectRpc(fail={"rtos.project.commit": RpcError(-32000, "no kernel ELF")})
    with pytest.raises(RpcError, match="no kernel ELF"):
        asyncio.run(load_project(rpc, root, "camelot"))
    assert not rpc.aborted
    assert "rtos.project.abort" not in rpc.methods


def test_load_project_rejects_an_unconfirmed_snapshot(tmp_path: Path) -> None:
    """A commit answer that does not confirm loading is an explicit failure."""
    root = tmp_path / "project"
    _make_project(root)
    rpc = FakeProjectRpc()
    rpc.commit_result = {"schema_version": 1, "loaded": False, "project_path": None}
    with pytest.raises(ProjectLoadError, match="did not confirm"):
        asyncio.run(load_project(rpc, root, "camelot"))
    assert not rpc.aborted


def test_dashboard_project_command_loads_and_summarizes(tmp_path: Path) -> None:
    """The project command transfers through the client RPC and reports the result."""
    root = tmp_path / "blinky project"
    _make_project(root)
    app = PyGdbClientApp("ws://127.0.0.1:1234")
    app._connected = True
    rpc = FakeProjectRpc()
    app.client.request = rpc.request  # type: ignore[method-assign]
    app._append_output = Mock()

    asyncio.run(app.execute_command(f'project "{root}" camelot'))

    assert rpc.methods[-1] == "rtos.project.commit"
    project = app._audit.project
    assert project is not None
    assert project.local_root == str(root.resolve())
    assert [component.name for component in project.description.components] == [
        "kernel",
        "runtime",
        "hello",
        "sample_c_app",
    ]
    messages = [call.args[0] for call in app._append_output.call_args_list]
    assert any(message.startswith("Loaded camelot project") for message in messages)
    assert any("tasks: blinky" in message for message in messages)
    assert not any("Audit context changed" in message for message in messages)

    app._agent = Mock(session=object())
    app.client.request = FakeProjectRpc().request  # type: ignore[method-assign]
    asyncio.run(app.execute_command(f'project "{root}" camelot'))
    messages = [call.args[0] for call in app._append_output.call_args_list]
    assert messages[-1].startswith("Audit context changed: use /new")


def test_dashboard_project_command_reports_usage_errors(tmp_path: Path) -> None:
    """Wrong arities show the usage with the supported types, without any RPC."""
    app = PyGdbClientApp("ws://127.0.0.1:1234")
    app._connected = True
    rpc = FakeProjectRpc()
    app.client.request = rpc.request  # type: ignore[method-assign]
    app._append_output = Mock()

    asyncio.run(app.execute_command("project"))

    assert rpc.requests == []
    messages = [call.args[0] for call in app._append_output.call_args_list]
    assert any("Usage: project <path> <type>" in message for message in messages)
