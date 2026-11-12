# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Local RTOS behavior and shared remote project contracts."""

import json
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from unittest.mock import Mock

import pytest

from pyGdbToolkit.shared.rtos import (
    ProjectManifest,
    ProjectRegistry,
    project_manifest,
    project_relative_path,
)
from pyGdbToolkit.shared.rtos.camelot import PRIVATE_BUILD, project_elf_paths
from pyGdbToolkit.cmd_rtos import RtosCmd, RtosSessionState, load_rtos_project, rtos_status
from pyGdbToolkit.rtos import camelot
from pyGdbToolkit.session import SESSION


def test_shared_manifest_import_does_not_load_gdb_services() -> None:
    """Host processes can import shared contracts even if GDB cannot be imported."""
    script = """
import importlib.abc
import sys

class RejectGdbServices(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "gdb":
            raise ModuleNotFoundError("GDB unavailable", name=fullname)
        if fullname.startswith("pyGdbToolkit.") and not (
            fullname == "pyGdbToolkit.shared" or fullname.startswith("pyGdbToolkit.shared.")
        ):
            raise AssertionError("Unexpected GDB service import: " + fullname)
        return None

sys.path.insert(0, sys.argv[1])
sys.meta_path.insert(0, RejectGdbServices())
from pyGdbToolkit.shared.rtos import project_manifest
import pyGdbServer.server
assert project_manifest("camelot").project_file == "project.toml"
assert "gdb" not in sys.modules
assert "pyGdbToolkit.session" not in sys.modules
assert not hasattr(sys.modules["pyGdbToolkit"], "SESSION")
"""
    subprocess.run(
        [sys.executable, "-I", "-c", script, str(Path(__file__).resolve().parents[1] / "src")],
        check=True,
    )


class ExampleProjectProvider:
    """Second RTOS contract with a non-TOML declaration and no Camelot paths."""

    name = "example"

    def manifest(self, project: bytes | None = None) -> ProjectManifest:
        """Expand a plain-text project declaration to its required ELF file."""
        files = ("context.txt",)
        if project is not None:
            relative = project_relative_path(project.decode("utf-8"))
            files += (relative,)
        return ProjectManifest("context.txt", files, ())


def test_registry_supports_additional_rtos_without_camelot_branches() -> None:
    """A provider owns its declaration format, paths and expansion behavior."""
    registry = ProjectRegistry()
    registry.register(ExampleProjectProvider())
    bootstrap = registry.manifest("example")
    assert bootstrap.project_file == "context.txt"
    assert bootstrap.required == ("context.txt",)
    assert registry.manifest("example", b"firmware.elf").required == ("context.txt", "firmware.elf")
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(ExampleProjectProvider())
    with pytest.raises(ValueError, match="Unsupported RTOS"):
        registry.manifest("camelot")
    with pytest.raises(ValueError, match="Unsupported RTOS"):
        project_manifest("example")


def test_manifest_expands_applications_from_project() -> None:
    """Remote transfer and local ELF discovery share the same ordered file names."""
    content = b"[kernel]\n[application.demo]\n[runtime]\n"
    manifest = project_manifest("camelot", content)
    assert manifest.required[:3] == (
        "project.toml",
        f"{PRIVATE_BUILD}/sentry-kernel.elf",
        f"{PRIVATE_BUILD}/demo-app.elf",
    )
    assert manifest.permits(f"{PRIVATE_BUILD}/demo-app.elf")
    assert not manifest.permits(f"{PRIVATE_BUILD}/demo-app.dummy.elf")
    assert manifest.permits("output/build/kernel/subprojects/kconfig-v1/generated_kconfig.json")
    assert not manifest.permits(
        "output/build/kernel/subprojects/kconfig-v1/extra/generated_kconfig.json"
    )
    assert not manifest.permits(
        "extra/output/build/kernel/subprojects/kconfig-v1/generated_kconfig.json"
    )


@pytest.mark.parametrize(
    "path", ["", "../x", "/tmp/x", "a//b", "a/./b", "a/../b", "a\\b", "C:x", "a\nb", 1]
)
def test_manifest_rejects_noncanonical_paths(path: object) -> None:
    """Project paths cannot escape staging or inject a command line."""
    with pytest.raises(ValueError):
        project_relative_path(path)


@pytest.mark.parametrize(
    "project",
    [
        {"application": []},
        {"application": {"../evil": {}}},
        {"application": {"..\\evil": {}}},
        {},
    ],
)
def test_invalid_project_elf_declarations(project: dict[str, object]) -> None:
    """Invalid projects fail rather than silently dropping their ELF files."""
    with pytest.raises(ValueError):
        project_elf_paths(project)


def test_remote_requires_kernel_but_local_application_only_is_unchanged() -> None:
    """The remote complete-context contract does not change standalone ELF discovery."""
    assert project_elf_paths({"application": {"demo": {}}}) == (f"{PRIVATE_BUILD}/demo-app.elf",)
    with pytest.raises(ValueError, match="kernel ELF"):
        project_manifest("camelot", b"[application.demo]\n")
    with pytest.raises(ValueError, match="Unsupported RTOS"):
        project_manifest("other")


def _project(tmp_path: Path) -> None:
    (tmp_path / "project.toml").write_text("[kernel]\n[application.demo]\n")
    for name in ("sentry-kernel.elf", "demo-app.elf"):
        path = tmp_path / PRIVATE_BUILD / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ELF fixture")


def _task_list() -> camelot.TaskList:
    return camelot.TaskList(
        0x8001000,
        64,
        32,
        (
            camelot.TaskSlot(
                0, 0x8001000, "demo", 0x8002000, 0x20000000, {"label": 42, "irqs": (1, 2)}
            ),
            camelot.TaskSlot(1, 0x8001020, None, 0, 0, {}),
        ),
    )


def test_status_is_json_serializable_and_only_reads_cached_state() -> None:
    """Empty and loaded sessions export nulls, numeric addresses and JSON arrays."""
    assert rtos_status() == {
        "schema_version": 1,
        "selected_rtos": None,
        "loaded": False,
        "project_path": None,
        "task_list": None,
        "scheduler_trace": None,
    }
    state = SESSION.state(RtosSessionState)
    state.selected_rtos = camelot
    state.project_path = Path("/nonexistent/project")
    state.task_list = _task_list()
    trace = Mock(limit=3, elections=["demo"])
    state.scheduler_trace = trace
    data = rtos_status()
    assert json.loads(json.dumps(data)) == data
    assert data["loaded"] is True
    assert data["project_path"] == "/nonexistent/project"
    assert data["scheduler_trace"] == {"limit": 3, "elections": ["demo"]}
    assert data["task_list"]["slots"][0]["metadata"] == {"label": 42, "irqs": [1, 2]}
    assert data["task_list"]["slots"][1]["task_name"] is None
    trace.elections.append("idle")
    assert data["scheduler_trace"]["elections"] == ["demo"]


def test_local_loader_and_rpc_entry_use_same_session(
    tmp_path: Path, fake_gdb: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GDB-only commands and remote loading discover identical symbols and task slots."""
    _project(tmp_path)
    execute = Mock(return_value="")
    monkeypatch.setattr(fake_gdb, "execute", execute, raising=False)
    metadata = Mock(return_value=_task_list())
    monkeypatch.setattr(camelot, "load_task_list", metadata)
    command = RtosCmd()
    command._invoke_select(["camelot"])
    assert rtos_status()["selected_rtos"] == "camelot"
    assert not rtos_status()["loaded"]
    command._invoke_load_project(["--from", str(tmp_path)])
    local = rtos_status()
    assert local["loaded"]
    assert execute.call_count == 2
    assert all(call.args[0].startswith('add-symbol-file "') for call in execute.call_args_list)
    assert load_rtos_project("camelot", str(tmp_path)) == local
    assert command.state is SESSION.state(RtosSessionState)
    command._invoke_select(["camelot"])
    assert not rtos_status()["loaded"]
    assert rtos_status()["task_list"] is None


def test_local_loader_failure_leaves_explicit_unloaded_state(
    tmp_path: Path, fake_gdb: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A metadata failure cannot appear as a successfully loaded context."""
    _project(tmp_path)
    monkeypatch.setattr(fake_gdb, "execute", Mock(return_value=""), raising=False)
    monkeypatch.setattr(camelot, "load_task_list", Mock(side_effect=ValueError("bad metadata")))
    with pytest.raises(fake_gdb.GdbError, match="bad metadata"):
        load_rtos_project("camelot", str(tmp_path))
    assert rtos_status()["selected_rtos"] == "camelot"
    assert rtos_status()["project_path"] is None
    assert not rtos_status()["loaded"]


def test_status_rejects_invalid_cached_metadata() -> None:
    """A mismatched RTOS task list is not silently serialized as an empty result."""
    with pytest.raises(ValueError, match="Invalid Camelot"):
        camelot.task_list_snapshot(object())
