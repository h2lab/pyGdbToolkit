# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Transfer, failure and GDB activation tests for the remote RTOS RPC API."""

import ast
import asyncio
import base64
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
from typing import Any
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from pyGdbToolkit.shared.rtos import (
    DEFAULT_PROJECT_REGISTRY,
    ProjectManifest,
    ProjectRegistry,
    project_manifest,
)
from pyGdbToolkit.shared.rtos.camelot import PRIVATE_BUILD, PROJECT_FILE
from pyGdbServer.config import ServerConfig
from pyGdbServer.mi import MiResult, MiSession
from pyGdbServer.rtos import CHUNK_SIZE, FILE_SIZE, RtosProjects
from pyGdbServer.server import PyGdbServer

PROJECT = b"[kernel]\n[application.demo]\nprovides = ['demo.elf']\n"
KCONFIG = "output/build/kernel/subprojects/kconfig-v1/generated_kconfig.json"


@pytest.fixture
def projects(tmp_path: Path) -> Iterator[RtosProjects]:
    """Keep uploaded files alive until the test finishes and then remove only staging."""
    store = RtosProjects(tmp_path)
    yield store
    store.close()


def _put(
    store: RtosProjects, path: str, data: bytes = b"fixture", name: str = "demo"
) -> dict[str, Any]:
    return store.put(name, path, 0, len(data), base64.b64encode(data).decode())


def _complete(store: RtosProjects, name: str = "demo") -> None:
    store.begin(name, "camelot")
    _put(store, PROJECT_FILE, PROJECT, name)
    for path in project_manifest("camelot", PROJECT).required[1:]:
        _put(store, path, name=name)
    _put(store, KCONFIG, name=name)


def _server(tmp_path: Path) -> PyGdbServer:
    return PyGdbServer(
        ServerConfig(
            ocd_path="openocd",
            ocd_args=(),
            gdb_path="gdb-multiarch",
            gdb_args=(),
            listen_host="127.0.0.1",
            listen_port=0,
            gdb_init=(),
            log_directory=tmp_path / "logs",
            startup_timeout=10,
        )
    )


def test_registry_exposes_supported_project_types() -> None:
    """Clients discover supported project types from the shared registry."""
    assert DEFAULT_PROJECT_REGISTRY.names == ("camelot",)


def test_complete_project_is_frozen_until_server_stops(projects: RtosProjects) -> None:
    """Manifest expansion, staging and activation retain a complete immutable project."""
    _complete(projects)
    rtos, path = projects.prepare("demo")
    assert rtos == "camelot"
    assert (path / PROJECT_FILE).read_bytes() == PROJECT
    projects.finish("demo", True)
    with pytest.raises(ValueError, match="frozen"):
        _put(projects, KCONFIG)
    with pytest.raises(ValueError, match="GDB may reference"):
        projects.abort("demo")
    with pytest.raises(ValueError, match="loading or loaded"):
        projects.prepare("demo")
    projects.close()
    assert not path.exists()


def test_staging_uses_registered_rtos_declaration_not_project_toml(tmp_path: Path) -> None:
    """The transfer layer does not assume Camelot paths or a TOML project format."""

    class ExampleProvider:
        name = "example"

        def manifest(self, project: bytes | None = None) -> ProjectManifest:
            files = ("config/context.json",)
            if project is not None:
                files += (json.loads(project)["elf"],)
            return ProjectManifest("config/context.json", files, ())

    store = RtosProjects(tmp_path, ProjectRegistry((ExampleProvider(),)))
    try:
        response = store.begin("demo", "example")
        assert response["manifest"]["project_file"] == "config/context.json"
        assert response["manifest"]["required"] == ["config/context.json"]
        with pytest.raises(ValueError, match="upload config/context.json first"):
            _put(store, "firmware.elf")
        _put(store, "config/context.json", b'{"elf":"firmware.elf"}')
        _put(store, "firmware.elf", b"ELF")
        rtos, path = store.prepare("demo")
        assert rtos == "example"
        assert (path / "firmware.elf").read_bytes() == b"ELF"
    finally:
        store.close()


def test_custom_project_declaration_has_same_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Declaration limits follow the registered provider's path, not project.toml."""
    import pyGdbServer.rtos as module

    class ExampleProvider:
        name = "example"

        def manifest(self, project: bytes | None = None) -> ProjectManifest:
            return ProjectManifest("config.txt", ("config.txt",), ())

    monkeypatch.setattr(module, "PROJECT_FILE_SIZE", 2)
    store = RtosProjects(tmp_path, ProjectRegistry((ExampleProvider(),)))
    try:
        store.begin("demo", "example")
        with pytest.raises(ValueError, match="between 0 and 2"):
            _put(store, "config.txt", b"abc")
    finally:
        store.close()


def test_chunked_file_is_only_published_when_complete(projects: RtosProjects) -> None:
    """Files larger than the workspace upload limit retain exact bytes across chunks."""
    projects.begin("demo", "camelot")
    _put(projects, PROJECT_FILE, PROJECT)
    data = b"x" * CHUNK_SIZE + b"y" * (2 * 1024 * 1024)
    path = f"{PRIVATE_BUILD}/sentry-kernel.elf"
    first = projects.put("demo", path, 0, len(data), base64.b64encode(data[:CHUNK_SIZE]).decode())
    assert first["next_offset"] == CHUNK_SIZE
    assert first["complete"] is False
    with pytest.raises(ValueError, match="incomplete"):
        projects.prepare("demo")
    second = projects.put(
        "demo", path, CHUNK_SIZE, len(data), base64.b64encode(data[CHUNK_SIZE:]).decode()
    )
    assert second["next_offset"] == len(data)
    assert second["complete"] is True
    # Inspect only the isolated project directory created by this store.
    roots = list(projects.workspace.glob("pygdb-rtos-*/project"))
    assert len(roots) == 1
    assert (roots[0] / path).read_bytes() == data
    assert not list((roots[0].parent / "chunks").iterdir())


@pytest.mark.parametrize("name", ["", "../escape", "/tmp/x", "a b", "a\nb", "é", "x" * 65, 3])
def test_reject_invalid_names(projects: RtosProjects, name: object) -> None:
    with pytest.raises(ValueError):
        projects.begin(name, "camelot")
    assert not list(projects.workspace.iterdir())


def test_duplicate_begin_cannot_destroy_staging(projects: RtosProjects) -> None:
    projects.begin("demo", "camelot")
    _put(projects, PROJECT_FILE, PROJECT)
    with pytest.raises(ValueError, match="already exists"):
        projects.begin("demo", "camelot")
    assert projects.abort("demo") == {"name": "demo", "aborted": True}
    projects.begin("demo", "camelot")


def test_application_elf_requires_completed_project_manifest(projects: RtosProjects) -> None:
    projects.begin("demo", "camelot")
    with pytest.raises(ValueError, match="manifest"):
        _put(projects, f"{PRIVATE_BUILD}/demo.elf")
    result = _put(projects, PROJECT_FILE, PROJECT)
    assert f"{PRIVATE_BUILD}/demo.elf" in result["manifest"]["required"]
    _put(projects, f"{PRIVATE_BUILD}/demo.elf")
    with pytest.raises(ValueError, match="manifest"):
        _put(projects, f"{PRIVATE_BUILD}/demo-app.elf")


@pytest.mark.parametrize(
    "content",
    [
        b"broken [toml",
        b"\xff",
        b"[application.demo]\nprovides = ['demo.elf']\n",
        b"[kernel]\n[application.demo]\n",
    ],
)
def test_invalid_project_can_be_corrected(projects: RtosProjects, content: bytes) -> None:
    projects.begin("demo", "camelot")
    with pytest.raises(ValueError):
        _put(projects, PROJECT_FILE, content)
    assert _put(projects, PROJECT_FILE, PROJECT)["complete"] is True


def test_invalid_manifest_does_not_collide_with_other_chunk_files(projects: RtosProjects) -> None:
    """Correcting project.toml must retain another file's unfinished chunks."""
    projects.begin("demo", "camelot")
    projects.put("demo", PROJECT_FILE, 0, 3, "YQ==")
    path = f"{PRIVATE_BUILD}/sentry-kernel.elf"
    projects.put("demo", path, 0, 2, "eA==")
    with pytest.raises(ValueError):
        projects.put("demo", PROJECT_FILE, 1, 3, "YmM=")
    _put(projects, PROJECT_FILE, PROJECT)
    assert projects.put("demo", path, 1, 2, "eQ==")["complete"] is True


@pytest.mark.parametrize(
    "offset,size,content",
    [
        (True, 1, "eA=="),
        (0, True, "eA=="),
        (-1, 1, "eA=="),
        (0, FILE_SIZE + 1, "eA=="),
        (0, 1, "not base64"),
        (0, 1, ""),
        (0, 0, "eA=="),
        (1, 2, "eA=="),
        (0, 1, 3),
    ],
)
def test_invalid_chunks_are_explicit_errors(
    projects: RtosProjects, offset: object, size: object, content: object
) -> None:
    projects.begin("demo", "camelot")
    with pytest.raises(ValueError):
        projects.put("demo", f"{PRIVATE_BUILD}/sentry-kernel.elf", offset, size, content)


def test_chunk_size_and_contiguous_offsets(projects: RtosProjects) -> None:
    projects.begin("demo", "camelot")
    path = f"{PRIVATE_BUILD}/sentry-kernel.elf"
    with pytest.raises(ValueError, match="chunk"):
        projects.put(
            "demo", path, 0, CHUNK_SIZE + 1, base64.b64encode(b"x" * (CHUNK_SIZE + 1)).decode()
        )
    projects.put("demo", path, 0, 3, "eA==")
    for offset, size in ((0, 3), (2, 3), (1, 4)):
        with pytest.raises(ValueError, match="constant"):
            projects.put("demo", path, offset, size, "eA==")
    result = projects.put("demo", path, 1, 3, "eHg=")
    assert result["complete"] is True
    with pytest.raises(ValueError, match="already complete"):
        _put(projects, path)


def test_total_reserved_size_is_bounded(
    projects: RtosProjects, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pyGdbServer.rtos as module

    monkeypatch.setattr(module, "PROJECT_SIZE", 3)
    projects.begin("demo", "camelot")
    projects.put("demo", f"{PRIVATE_BUILD}/sentry-kernel.elf", 0, 3, "eA==")
    with pytest.raises(ValueError, match="project must not exceed"):
        _put(projects, f"{PRIVATE_BUILD}/layout.json", b"x")


def test_project_and_file_counts_are_bounded(
    projects: RtosProjects, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pyGdbServer.rtos as module

    monkeypatch.setattr(module, "MAX_PROJECTS", 1)
    monkeypatch.setattr(module, "MAX_FILES", 1)
    projects.begin("demo", "camelot")
    with pytest.raises(ValueError, match="at most"):
        projects.begin("another", "camelot")
    _put(projects, PROJECT_FILE, PROJECT)
    with pytest.raises(ValueError, match="files"):
        _put(projects, f"{PRIVATE_BUILD}/layout.json")


def test_manifest_requires_exactly_one_kernel_config(projects: RtosProjects) -> None:
    _complete(projects)
    _put(projects, KCONFIG.replace("kconfig-v1", "kconfig-v2"))
    with pytest.raises(ValueError, match="exactly one"):
        projects.prepare("demo")


def test_missing_config_and_modified_files_are_rejected(projects: RtosProjects) -> None:
    _complete(projects)
    root = next(projects.workspace.glob("pygdb-rtos-*/project"))
    (root / KCONFIG).unlink()
    with pytest.raises(ValueError, match="missing or changed"):
        projects.prepare("demo")
    (root / KCONFIG).write_bytes(b"fixture")
    (root / PRIVATE_BUILD / "sentry-kernel.elf").write_bytes(b"truncated fixture")
    with pytest.raises(ValueError, match="missing or changed"):
        projects.prepare("demo")


def test_put_reports_filesystem_failure(
    projects: RtosProjects, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects.begin("demo", "camelot")

    def denied(path: Path, *args: object, **kwargs: object) -> None:
        raise PermissionError("staging is read-only")

    monkeypatch.setattr(Path, "mkdir", denied)
    with pytest.raises(PermissionError, match="read-only"):
        _put(projects, PROJECT_FILE, PROJECT)
    monkeypatch.undo()
    projects.abort("demo")


def test_symlink_cannot_escape_staging(projects: RtosProjects, tmp_path: Path) -> None:
    projects.begin("demo", "camelot")
    _put(projects, PROJECT_FILE, PROJECT)
    root = next(projects.workspace.glob("pygdb-rtos-*/project"))
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "output").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="inside staging"):
        _put(projects, f"{PRIVATE_BUILD}/layout.json")
    assert not list(outside.iterdir())


def test_concurrent_same_name_only_allocates_one_project(projects: RtosProjects) -> None:
    def begin() -> bool:
        try:
            projects.begin("demo", "camelot")
        except ValueError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: begin(), range(2)))
    assert sorted(results) == [False, True]
    assert len(list(projects.workspace.glob("pygdb-rtos-*"))) == 1


def test_rpc_transfer_commit_and_status_over_websocket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise JSON-RPC wire shapes and ensure activation is one serialized GDB command."""
    server = _server(tmp_path)
    monkeypatch.setattr(server, "workspace", tmp_path)
    server.rtos_projects = RtosProjects(tmp_path)
    snapshot: dict[str, object] = {
        "schema_version": 1,
        "selected_rtos": "camelot",
        "loaded": True,
        "project_path": None,
        "task_list": None,
        "scheduler_trace": None,
    }

    async def console(command: str, timeout: float) -> MiResult:
        if "load_rtos_project(" in command:
            tree = ast.parse(command.removeprefix("python "))
            call = next(
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "load_rtos_project"
            )
            snapshot["project_path"] = ast.literal_eval(call.args[1])
        return MiResult("done", "done", ("PYGDBSERVER_RTOS_JSON:" + json.dumps(snapshot),))

    console_mock = AsyncMock(side_effect=console)
    monkeypatch.setattr(server.mi, "console", console_mock)

    async def exercise() -> None:
        async with serve(server._handle_connection, "127.0.0.1", 0) as listener:
            async with connect(f"ws://127.0.0.1:{listener.sockets[0].getsockname()[1]}") as socket:
                identifier = 0

                async def rpc(
                    method: str, params: dict[str, object] | None = None
                ) -> dict[str, Any]:
                    nonlocal identifier
                    identifier += 1
                    await socket.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": identifier,
                                "method": method,
                                "params": params or {},
                            }
                        )
                    )
                    response = json.loads(await socket.recv())
                    assert response["id"] == identifier
                    assert "error" not in response, response
                    return response["result"]

                begin = await rpc("rtos.project.begin", {"name": "demo", "rtos": "camelot"})
                assert begin["limits"]["chunk_size"] == CHUNK_SIZE
                assert not console_mock.called
                inputs = {
                    path: b"fixture" for path in project_manifest("camelot", PROJECT).required
                }
                inputs[PROJECT_FILE] = PROJECT
                inputs[KCONFIG] = b'{"CONFIG_SVC_EXCHANGE_AREA_LEN":128}'
                for path, data in inputs.items():
                    await rpc(
                        "rtos.project.put",
                        {
                            "name": "demo",
                            "path": path,
                            "offset": 0,
                            "size": len(data),
                            "content": base64.b64encode(data).decode(),
                        },
                    )
                assert not console_mock.called
                loaded = await rpc("rtos.project.commit", {"name": "demo", "timeout": 60})
                assert loaded["loaded"] is True
                assert console_mock.call_count == 1
                assert console_mock.call_args.args[1] == 60
                assert await rpc("rtos.status") == loaded
        server.rtos_projects.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("output", [None, {}, {"schema_version": 1, "loaded": False}])
def test_commit_missing_or_failure_snapshot_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: object
) -> None:
    server = _server(tmp_path)
    server.rtos_projects = RtosProjects(tmp_path)
    _complete(server.rtos_projects)
    monkeypatch.setattr(server, "_python_json", AsyncMock(return_value=output))
    with pytest.raises(RuntimeError, match="did not confirm"):
        asyncio.run(server.rtos_project_commit("demo"))
    with pytest.raises(ValueError, match="GDB may reference"):
        server.rtos_projects.abort("demo")
    # Retrying the identical frozen project is allowed after an activation failure.
    server.rtos_projects.prepare("demo")
    server.rtos_projects.close()


@pytest.mark.parametrize(
    "method,params,code",
    [
        ("rtos.project.begin", {"name": "demo", "rtos": "unknown"}, -32602),
        ("rtos.project.begin", {"name": "../x", "rtos": "camelot"}, -32602),
        ("rtos.project.begin", {"name": "demo"}, -32602),
        ("rtos.project.commit", {"name": "unknown"}, -32602),
        ("rtos.status", {"directory": "/tmp"}, -32602),
    ],
)
def test_rpc_validation_errors_do_not_call_gdb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    params: dict[str, object],
    code: int,
) -> None:
    server = _server(tmp_path)
    console = AsyncMock()
    monkeypatch.setattr(server.mi, "console", console)
    response, _ = asyncio.run(
        server.handle_rpc_message(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        )
    )
    assert response["error"]["code"] == code
    console.assert_not_called()


def test_rpc_gdb_failure_is_reported_and_status_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _server(tmp_path)
    server.rtos_projects = RtosProjects(tmp_path)
    _complete(server.rtos_projects)
    console = AsyncMock(side_effect=RuntimeError("GDB metadata error"))
    monkeypatch.setattr(server.mi, "console", console)

    async def exercise() -> None:
        for method, params in (
            ("rtos.project.commit", {"name": "demo"}),
            ("rtos.status", {}),
        ):
            response, _ = await server.handle_rpc_message(
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            )
            assert response["error"] == {"code": -32000, "message": "GDB metadata error"}
        server.rtos_projects.close()

    asyncio.run(exercise())
    assert console.call_count == 2


def test_status_missing_payload_is_an_explicit_rpc_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _server(tmp_path)
    monkeypatch.setattr(server.mi, "console", AsyncMock(return_value=MiResult("done", "done", ())))
    response, _ = asyncio.run(
        server.handle_rpc_message(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "rtos.status"}))
    )
    assert response["error"]["code"] == -32000
    assert "structured RTOS" in response["error"]["message"]


@pytest.mark.skipif(shutil.which("gdb-multiarch") is None, reason="gdb-multiarch unavailable")
def test_real_gdb_status_without_target(tmp_path: Path) -> None:
    """The structured exporter also works in a standalone real GDB without hardware."""
    server = _server(tmp_path)
    server.mi = MiSession("gdb-multiarch", (), server.logs)
    source = str(Path(__file__).resolve().parents[1] / "src")

    async def exercise() -> None:
        await server.mi.start(10)
        try:
            await server.mi.console(
                f"python import sys; sys.path.insert(0, {source!r}); import pyGdbToolkit"
            )
            status = await server.rtos_status()
            assert status["selected_rtos"] is None
            assert status["loaded"] is False
            await server.mi.console("rtos select camelot")
            status = await server.rtos_status()
            assert status["selected_rtos"] == "camelot"
            assert status["loaded"] is False
        finally:
            await server.mi.stop()
            server.rtos_projects.close()

    asyncio.run(exercise())
