# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Workspace authorization, Zephyr source aliases and local-only LLM source tools."""

import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from pyGdbClient.app import PyGdbClientApp
from pyGdbClient.audit import AuditContextManager
from pyGdbClient.llm.controller import AgentController, AgentSettings
from pyGdbClient.llm.sources import SourceBrowser, SourceError
from pyGdbClient.project import ProjectLoadError, load_project
from pyGdbClient.project_sources import SourceRoot, SourceWorkspace, discover_sources
from pyGdbClient.rpc import JsonRpcClient
from pyGdbToolkit.shared.rtos.zephyr import REQUIRED
from rpc_fakes import FakeRpcServer
from test_audit import _responses
from test_llm_contracts import _context
from test_llm_opencode import Server, answer, provider
from test_project import FakeProjectRpc, _make_project


@pytest.fixture
def build(tmp_path: Path) -> Path:
    """Create distinct sources plus an unrelated workspace directory that must stay hidden."""
    root = tmp_path / "build"
    for path in ("build/zephyr", "zephyr/kernel", "app/src", "modules/hal/stm32", "private"):
        (tmp_path / path).mkdir(parents=True, exist_ok=True)
    for path, content in (
        ("app/src/main.c", "app_function();\n"),
        ("zephyr/kernel/sched.c", "kernel_function();\n"),
        ("modules/hal/stm32/hal.c", "module_function();\n"),
        ("private/secret.c", "must never be exposed\n"),
        ("build/zephyr/generated.h", "generated_function();\n"),
    ):
        (tmp_path / path).write_text(content)
    data = {
        "version": "0.1.0",
        "cmake": {
            "application": {"source-dir": str(tmp_path / "app")},
            "board": {"name": "board"},
            "zephyr": {"version": "4.5.99", "zephyr-base": str(tmp_path / "zephyr")},
        },
    }
    for relative in REQUIRED:
        (root / relative).write_bytes(
            yaml.safe_dump(data).encode() if relative == "build_info.yml" else b"artifact"
        )
    (root / "zephyr_modules.txt").write_text(
        f'"hal_stm32":{json.dumps(str(tmp_path / "modules/hal/stm32"))}:"${{IGNORED}}"\n'
    )
    return root


def _access(build: Path) -> SourceWorkspace:
    return discover_sources(build, build.parent, (build / "build_info.yml").read_bytes())


def _call(browser: SourceBrowser, name: str, path: str, **kwargs: object) -> dict[str, object]:
    arguments = {"path": path, "offset": 1, "limit": 100, **kwargs}
    return browser.execute(name, arguments)


def test_alias_read_glob_grep_and_audit_discovery_provenance(build: Path) -> None:
    """Only explicit build plus metadata-declared subtrees are exposed, not workspace siblings."""
    access = _access(build)
    assert [root.alias for root in access.roots] == [
        "build",
        "application",
        "zephyr",
        "module-hal_stm32",
    ]
    assert access.roots[1].directory == "app"
    browser = SourceBrowser.for_workspace(access)
    listing = _call(browser, "glob", ".", pattern="*")
    assert listing["root_listing_only"] is True
    assert {item["path"] for item in listing["matches"]} == {
        "build",
        "application",
        "zephyr",
        "module-hal_stm32",
    }
    for path, text in (
        ("application/src/main.c", "app_function"),
        ("zephyr/kernel/sched.c", "kernel_function"),
        ("module-hal_stm32/hal.c", "module_function"),
        ("build/zephyr/generated.h", "generated_function"),
    ):
        result = _call(browser, "read", path)
        assert text in result["content"] and result["path"] == path
    assert _call(browser, "glob", "application", pattern="**/*.c")["matches"] == [
        {"path": "application/src/main.c"}
    ]
    result = _call(browser, "grep", "zephyr", pattern="kernel_function", glob="**/*.c")
    assert result["matches"] == [
        {"path": "zephyr/kernel/sched.c", "line": 1, "text": "kernel_function();"}
    ]
    assert (
        dict(access.metadata)["zephyr_modules.txt"]
        == hashlib.sha256((build / "zephyr_modules.txt").read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    "path",
    [
        "private/secret.c",
        "../private/secret.c",
        "application/../../private/secret.c",
        "module-hal_stm32/../../../private/secret.c",
        "/etc/passwd",
        "app/src/main.c",
        "application/.git/config",
    ],
)
def test_tool_paths_cannot_escape_authorized_aliases(build: Path, path: str) -> None:
    """Neither workspace-relative physical paths nor parent traversal grant extra access."""
    with pytest.raises(SourceError):
        _call(SourceBrowser.for_workspace(_access(build)), "read", path)


def test_workspace_root_is_listing_only_never_a_cross_root_scan(build: Path) -> None:
    """Per-scan budgets cannot be multiplied by scanning every root in one call."""
    browser = SourceBrowser.for_workspace(_access(build))
    with pytest.raises(SourceError, match="select a source alias"):
        _call(browser, "grep", ".", pattern="function")
    assert _call(browser, "glob", ".", pattern="**/*.c")["matches"] == []


@pytest.mark.parametrize("when", ["before-session", "after-session"])
def test_workspace_ancestor_symlink_is_rejected_even_when_inodes_match(
    tmp_path: Path, when: str
) -> None:
    """Every boundary ancestor is opened without following links, not just the leaf."""
    boundary = tmp_path / "workspace"
    source = boundary / "app"
    source.mkdir(parents=True)
    parent_identity, source_identity = boundary.stat(), source.stat()
    access = SourceWorkspace(
        boundary,
        parent_identity.st_dev,
        parent_identity.st_ino,
        (SourceRoot("application", "app", source_identity.st_dev, source_identity.st_ino),),
        (),
    )
    browser = SourceBrowser.for_workspace(access) if when == "after-session" else None
    previous = tmp_path.with_name(tmp_path.name + "-previous")
    tmp_path.rename(previous)
    try:
        tmp_path.symlink_to(previous, target_is_directory=True)
        with pytest.raises(SourceError):
            if browser is None:
                SourceBrowser.for_workspace(access)
            else:
                _call(browser, "glob", ".", pattern="*")
    finally:
        tmp_path.unlink()
        previous.rename(tmp_path)


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_symlinks_inside_sources_are_not_followed(build: Path, kind: str) -> None:
    """Reusing the existing fd-relative browser keeps link/special-file protection."""
    target = build.parent / "private"
    link = build.parent / "app" / "link"
    link.symlink_to(target if kind == "directory" else target / "secret.c")
    with pytest.raises(SourceError):
        _call(
            SourceBrowser.for_workspace(_access(build)),
            "read",
            "application/link/secret.c" if kind == "directory" else "application/link",
        )


@pytest.mark.parametrize("when", ["before-session", "after-session"])
@pytest.mark.parametrize("replacement", ["directory", "symlink", "ancestor", "workspace"])
def test_identity_replacement_refuses_stale_source_authorization(
    build: Path, when: str, replacement: str
) -> None:
    """Authorization pins workspace and source directories, including intervening ancestors."""
    access = _access(build)
    browser = SourceBrowser.for_workspace(access) if when == "after-session" else None
    boundary = build.parent
    root = (
        boundary
        if replacement == "workspace"
        else (boundary / "modules" if replacement == "ancestor" else boundary / "app")
    )
    root.rename(root.with_name(root.name + "-previous"))
    if replacement == "symlink":
        root.symlink_to(boundary / "private", target_is_directory=True)
    else:
        root.mkdir()
        if replacement == "ancestor":
            (root / "hal/stm32").mkdir(parents=True)
    with pytest.raises(SourceError):
        if browser is None:
            SourceBrowser.for_workspace(access)
        else:
            _call(browser, "glob", ".", pattern="*")


@pytest.mark.parametrize(
    "record",
    [
        "not a module record",
        '"hal_stm32":"../outside":"x"',
        '"hal_stm32":"/etc":"x"',
        '"../escape":"/tmp":"x"',
        '"hal_stm32":"/tmp":"x"\n"hal_stm32":"/tmp":"x"',
        '"bad":"\\u0000":"x"',
        '"bad":"x":"x": "unexpected"',
        r'"bad":"\q":"unused"',
    ],
)
def test_invalid_module_metadata_fails_before_any_rpc(build: Path, record: str) -> None:
    """Never execute module metadata, shell placeholders or use its paths as permission."""
    (build / "zephyr_modules.txt").write_text(record)
    rpc = FakeProjectRpc(rtos="zephyr")
    with pytest.raises(ProjectLoadError, match="cannot authorize"):
        asyncio.run(load_project(rpc, build, "zephyr", workspace=build.parent))
    assert rpc.requests == []


@pytest.mark.parametrize("field", ["application", "zephyr"])
def test_application_and_kernel_cannot_escape_or_expose_entire_workspace(
    build: Path, field: str
) -> None:
    """Every root, not just modules, is confined to the auditor-selected boundary."""
    metadata = build / "build_info.yml"
    data = yaml.safe_load(metadata.read_bytes())
    key = "source-dir" if field == "application" else "zephyr-base"
    for path in (str(build.parent.parent), str(build.parent), str(build.parent / "app/../private")):
        data["cmake"][field][key] = path
        metadata.write_text(yaml.safe_dump(data))
        with pytest.raises(ValueError):
            _access(build)


def test_missing_metadata_and_symlinked_roots_fail_closed(build: Path) -> None:
    """Missing inventories and unsafe links fail rather than silently authorizing no modules."""
    modules = build / "zephyr_modules.txt"
    content = modules.read_bytes()
    modules.unlink()
    with pytest.raises(SourceError):
        _access(build)
    modules.write_bytes(content)
    app = build.parent / "app"
    app.rename(build.parent / "app-original")
    app.symlink_to(build.parent / "app-original", target_is_directory=True)
    with pytest.raises(ValueError, match="cannot authorize.*application"):
        _access(build)


def test_module_metadata_must_be_a_bounded_regular_file(build: Path) -> None:
    """Metadata uses the same no-link, no-special-file and byte-budget checks as source reads."""
    modules = build / "zephyr_modules.txt"
    modules.unlink()
    modules.symlink_to(build.parent / "private/secret.c")
    with pytest.raises(SourceError, match="regular files"):
        _access(build)
    modules.unlink()
    modules.write_bytes(b"x" * (256 * 1024 + 1))
    with pytest.raises(SourceError, match="exceeds"):
        _access(build)


@pytest.mark.parametrize("count", [256, 257])
def test_exact_module_count_budget(build: Path, count: int) -> None:
    """Exercise the actual 256-module cap, with unique aliases over one authorized tree."""
    directory = json.dumps(str(build.parent / "modules/hal/stm32"))
    (build / "zephyr_modules.txt").write_text(
        "".join(f'"module{i}":{directory}:"unused"\n' for i in range(count))
    )
    if count == 256:
        access = _access(build)
        assert len(access.roots) == 259
        result = _call(SourceBrowser.for_workspace(access), "glob", ".", pattern="*")
        assert len(result["matches"]) == 100
        assert result["truncated"] is True
        result = _call(SourceBrowser.for_workspace(access), "glob", ".", pattern="module-module25?")
        assert len(result["matches"]) == 6 and result["truncated"] is False
    else:
        with pytest.raises(ValueError, match="exceeds 256"):
            _access(build)


def test_empty_module_inventory_is_valid_and_duplicate_names_are_not(build: Path) -> None:
    """A real empty inventory is different from an unavailable inventory."""
    modules = build / "zephyr_modules.txt"
    modules.write_text("")
    assert len(_access(build).roots) == 3
    record = f'"duplicate":{json.dumps(str(build.parent / "app"))}:"unused"\n'
    modules.write_text(record * 2)
    with pytest.raises(ValueError, match="duplicate"):
        _access(build)


def test_workspace_and_metadata_paths_are_never_inferred_or_remapped(build: Path) -> None:
    """Copied builds need matching local paths; stale absolute paths are not guessed."""
    declaration = (build / "build_info.yml").read_bytes()
    with pytest.raises(ValueError, match="changed during"):
        discover_sources(build, build.parent, declaration + b"# changed")
    with pytest.raises(ValueError, match="inside"):
        discover_sources(build, build / "zephyr", declaration)
    with pytest.raises(ValueError, match="ancestor"):
        discover_sources(build, build, declaration)
    link = build.parent / "workspace-link"
    link.symlink_to(build.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic links"):
        discover_sources(build, link, declaration)
    with pytest.raises(ValueError, match="filesystem root"):
        discover_sources(build, Path("/"), declaration)


def test_module_inventory_must_be_utf8(build: Path) -> None:
    """Invalid byte sequences are reported rather than silently dropping modules."""
    (build / "zephyr_modules.txt").write_bytes(b"\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        _access(build)


def test_project_transfer_and_audit_keep_source_authorization_client_side(build: Path) -> None:
    """Transfer four artifacts only, never source files or workspace permission."""
    rpc = FakeProjectRpc(rtos="zephyr")
    loaded = asyncio.run(load_project(rpc, build, "zephyr", workspace=build.parent))
    assert loaded.source_workspace is not None
    assert set(rpc.files) == set(REQUIRED)
    assert all("workspace" not in params for _, params in rpc.requests)
    context = AuditContextManager().set_project(loaded)
    access = context.to_json()["source_access"]
    assert access["workspace"] == str(build.parent)
    assert len(access["roots"]) == 4
    assert "not verified" in access["revision_status"]
    assert all(component.sources is None for component in context.description.components)
    assert loaded.source_workspace is context.source_workspace
    # Metadata authorization is opt-in: the legacy build-only workflow remains intact.
    loaded_without = asyncio.run(load_project(FakeProjectRpc(rtos="zephyr"), build, "zephyr"))
    assert loaded_without.source_workspace is None


def test_dashboard_parses_explicit_workspace_and_reports_authorized_aliases(build: Path) -> None:
    """The UI uses the same authorization path and records it for the next agent session."""
    app = PyGdbClientApp("ws://127.0.0.1:1234")
    app._connected = True
    rpc = FakeProjectRpc(rtos="zephyr")
    app.client.request = rpc.request  # type: ignore[method-assign]
    app._append_output = Mock()
    asyncio.run(app.execute_command(f'project "{build}" zephyr --workspace "{build.parent}"'))
    assert app._audit.project.source_workspace is not None
    messages = [call.args[0] for call in app._append_output.call_args_list]
    assert any(
        "Authorized source workspace:" in message and "module-hal_stm32" in message
        for message in messages
    )


def test_nonzephyr_workspace_option_is_rejected_before_rpc(tmp_path: Path) -> None:
    """The explicit workspace extension does not alter Camelot authorization."""
    _make_project(tmp_path)
    rpc = FakeProjectRpc()
    with pytest.raises(ProjectLoadError, match="only supported for Zephyr"):
        asyncio.run(load_project(rpc, tmp_path, "camelot", workspace=tmp_path.parent))
    assert rpc.requests == []


def test_llm_uses_aliases_through_local_tools_and_stale_project_is_refused(build: Path) -> None:
    """Exercise local source tools without handing filesystem access to a model."""

    def proposal(call_id: str, name: str, arguments: dict[str, object]) -> str:
        return json.dumps(
            {"type": "tool_calls", "calls": [{"id": call_id, "name": name, "arguments": arguments}]}
        )

    async def run() -> None:
        responses = _responses() | {"target.context": asdict(_context())}
        loaded = await load_project(
            FakeProjectRpc(rtos="zephyr"), build, "zephyr", workspace=build.parent
        )
        manager = AuditContextManager()
        manager.set_project(loaded)
        model = Server(
            proposal("glob1", "glob", {"path": ".", "pattern": "*"}),
            proposal("read1", "read", {"path": "application/src/main.c"}),
            proposal("grep1", "grep", {"path": "zephyr", "pattern": "kernel_function"}),
            proposal("read2", "read", {"path": "module-hal_stm32/hal.c"}),
            proposal("denied", "read", {"path": "private/secret.c"}),
            answer("Analysis complete."),
        )
        async with FakeRpcServer(responses) as target:
            client = JsonRpcClient(target.url)
            await client.connect()
            controller = AgentController(
                AgentSettings(provider(model)),
                client,
                approver=Mock(),
                sink=lambda event: None,
                audit=manager,
            )
            try:
                result = await controller.submit("Inspect application, kernel and module.")
                assert result.reason == "completed" and result.tool_calls == 5
                assert "app_function();" in model.prompts[-1]
                assert "module_function();" in model.prompts[-1]
                assert "kernel_function();" in model.prompts[-1]
                assert "authorized source alias" in model.prompts[-1]
                assert "must never be exposed" not in model.prompts[-1]
                assert not any(method.startswith("local.source") for method in target.methods)
                manager.clear_project()
                model.answers.extend(
                    (
                        proposal("stale", "read", {"path": "application/src/main.c"}),
                        answer("Denied"),
                    )
                )
                await controller.submit("Read again")
                assert "loaded project changed" in model.prompts[-1]
            finally:
                await controller.close()
                await client.close()

    asyncio.run(run())
