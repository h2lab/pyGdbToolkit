# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""RTOS-neutral dispatch and Camelot scheduler lifecycle regression tests."""

from collections.abc import Callable
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest
from rich.console import Console, Group

from pyGdbToolkit.cmd_rtos import RtosCmd, load_rtos_project, rtos_status
from pyGdbToolkit.rtos import SUPPORTED_RTOS, camelot
from pyGdbToolkit.rtos.base import RtosCapabilities, SchedulerTrace
from pyGdbToolkit.shared.rtos import TaskListSnapshot


@dataclass
class ExampleTrace:
    """Trace with no GDB breakpoint or Camelot election semantics."""

    limit: int
    on_delete: Callable[[SchedulerTrace], None]
    elections: list[str] = field(default_factory=list)
    deleted: bool = False

    def delete(self) -> None:
        """Release the trace and notify its owning session."""
        self.deleted = True
        self.on_delete(self)


@dataclass
class ExampleProject:
    """Loaded project with dynamic tasks, not a static Camelot task table."""

    capabilities: RtosCapabilities = RtosCapabilities(True, True)
    shown_tasks: list[str] = field(default_factory=list)

    def task_list_snapshot(self) -> TaskListSnapshot | None:
        """Report that a static task table is not applicable."""
        return None

    def show_project(self) -> Group:
        """Show cached example build information."""
        return Group("Example build")

    def show_task(self, name: str) -> Group:
        """Record a live inspection request."""
        self.shown_tasks.append(name)
        return Group(f"Example task {name}")

    def trace_scheduler(
        self, limit: int, console: Console, on_delete: Callable[[SchedulerTrace], None]
    ) -> ExampleTrace:
        """Start observation without sched_elect or a returned handle."""
        del console
        return ExampleTrace(limit, on_delete)


@dataclass
class ExampleRtos:
    """Second RTOS implementation using a single differently named image."""

    project: ExampleProject
    name = "example"

    def project_elfs(self, project_path: Path) -> list[Path]:
        """Discover the primary image without Camelot-specific paths."""
        elf = project_path / "image.elf"
        if not elf.is_file():
            raise FileNotFoundError(elf)
        return [elf]

    def load_project(self, project_path: Path, primary_elf: Path) -> ExampleProject:
        """Check dispatch arguments and return a context without a static table."""
        assert primary_elf == project_path / "image.elf"
        return self.project


@pytest.fixture
def example_project(
    tmp_path: Path, fake_gdb: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> ExampleProject:
    """Register a second RTOS with a usable image but no target dependency."""
    project = ExampleProject()
    (tmp_path / "image.elf").write_bytes(b"example ELF")
    monkeypatch.setitem(SUPPORTED_RTOS, "example", ExampleRtos(project))
    monkeypatch.setattr(fake_gdb, "execute", Mock(return_value=""), raising=False)
    return project


def test_second_rtos_dispatches_without_static_tasks(
    example_project: ExampleProject,
    tmp_path: Path,
    fake_gdb: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Load, status and show do not require a Camelot image or task-list snapshot."""
    snapshot = load_rtos_project("example", str(tmp_path))
    assert snapshot == {
        "schema_version": 1,
        "selected_rtos": "example",
        "loaded": True,
        "project_path": str(tmp_path),
        "task_list": None,
        "scheduler_trace": None,
    }
    fake_gdb.execute.assert_called_once_with(
        f'add-symbol-file "{tmp_path}/image.elf" -o 0', from_tty=False, to_string=True
    )
    command = RtosCmd()
    command._invoke_list([])
    command._invoke_show([])
    command._invoke_show_task(["worker"])
    assert command.state.project is example_project
    assert example_project.shown_tasks == ["worker"]
    output = capsys.readouterr().out
    assert "Selected" in output
    assert "Example build" in output
    assert "Example task worker" in output


@pytest.mark.parametrize("operation", ["task", "scheduler"])
def test_build_capabilities_gate_live_operations(
    operation: str,
    example_project: ExampleProject,
    tmp_path: Path,
    fake_gdb: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsupported operations fail explicitly without touching the live backend."""
    example_project.capabilities = RtosCapabilities(False, False)
    load_rtos_project("example", str(tmp_path))
    inspect = Mock(side_effect=AssertionError("must not inspect"))
    trace = Mock(side_effect=AssertionError("must not trace"))
    monkeypatch.setattr(example_project, "show_task", inspect)
    monkeypatch.setattr(example_project, "trace_scheduler", trace)
    command = RtosCmd()
    with pytest.raises(fake_gdb.GdbError, match="unavailable for this example build"):
        if operation == "task":
            command._invoke_show_task(["worker"])
        else:
            command._invoke_showsched(["2"])
    inspect.assert_not_called()
    trace.assert_not_called()
    assert rtos_status()["loaded"]


@pytest.mark.parametrize("operation", ["select", "load", "reset", "replace"])
def test_session_cancels_implementation_owned_trace(
    operation: str, example_project: ExampleProject, tmp_path: Path
) -> None:
    """Every project lifecycle transition releases the current trace."""
    load_rtos_project("example", str(tmp_path))
    command = RtosCmd()
    command._invoke_showsched(["3"])
    trace = command.state.scheduler_trace
    assert isinstance(trace, ExampleTrace)
    trace.elections.append("worker")
    assert rtos_status()["scheduler_trace"] == {"limit": 3, "elections": ["worker"]}
    if operation == "select":
        command._invoke_select(["camelot"])
    elif operation == "load":
        command._invoke_load_project(["--from", str(tmp_path)])
    elif operation == "reset":
        command.state.reset()
    else:
        command._invoke_showsched(["1"])
    assert trace.deleted
    if operation == "replace":
        replacement = command.state.scheduler_trace
        assert replacement is not None
        assert replacement is not trace
        trace.delete()
        assert command.state.scheduler_trace is replacement
    else:
        assert command.state.scheduler_trace is None


def test_trace_creation_failure_is_explicit(
    example_project: ExampleProject,
    tmp_path: Path,
    fake_gdb: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing a trace with a failed backend leaves no apparently active trace."""
    load_rtos_project("example", str(tmp_path))
    command = RtosCmd()
    command._invoke_showsched(["2"])
    previous = command.state.scheduler_trace
    monkeypatch.setattr(
        example_project, "trace_scheduler", Mock(side_effect=ValueError("hook unavailable"))
    )
    with pytest.raises(fake_gdb.GdbError, match="Cannot trace RTOS scheduler: hook unavailable"):
        command._invoke_showsched(["2"])
    assert isinstance(previous, ExampleTrace) and previous.deleted
    assert rtos_status()["scheduler_trace"] is None


@pytest.mark.parametrize("failure", ["empty", "discovery", "symbols", "metadata"])
def test_load_failures_do_not_retain_a_loaded_context(
    failure: str,
    example_project: ExampleProject,
    tmp_path: Path,
    fake_gdb: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed replacement clears the old context and releases its active trace."""
    load_rtos_project("example", str(tmp_path))
    command = RtosCmd()
    command._invoke_showsched(["1"])
    trace = command.state.scheduler_trace
    backend = SUPPORTED_RTOS["example"]
    if failure == "empty":
        monkeypatch.setattr(backend, "project_elfs", Mock(return_value=[]))
    elif failure == "discovery":
        monkeypatch.setattr(backend, "project_elfs", Mock(side_effect=ValueError("bad project")))
    elif failure == "symbols":
        fake_gdb.execute.side_effect = fake_gdb.error("bad symbols")
    else:
        monkeypatch.setattr(backend, "load_project", Mock(side_effect=ValueError("bad metadata")))
    with pytest.raises(fake_gdb.GdbError, match="Cannot load RTOS"):
        command._invoke_load_project(["--from", str(tmp_path)])
    assert isinstance(trace, ExampleTrace) and trace.deleted
    assert command.state.project is None
    assert rtos_status()["loaded"] is False
    assert rtos_status()["scheduler_trace"] is None


def test_camelot_still_requires_kernel_before_loading_symbols(
    tmp_path: Path, fake_gdb: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Camelot-specific image requirement now belongs to its implementation."""
    (tmp_path / "project.toml").write_text('[application.demo]\nprovides = ["demo-app.elf"]\n')
    elf = tmp_path / "output/build/camelot_private/demo-app.elf"
    elf.parent.mkdir(parents=True)
    elf.write_bytes(b"ELF fixture")
    execute = Mock()
    monkeypatch.setattr(fake_gdb, "execute", execute, raising=False)
    with pytest.raises(fake_gdb.GdbError, match="kernel ELF is required"):
        load_rtos_project("camelot", str(tmp_path))
    execute.assert_not_called()
    assert not rtos_status()["loaded"]


@dataclass
class BreakpointHarness:
    """Record GDB breakpoint validity, arguments and deferred deletion events."""

    created: list[object] = field(default_factory=list)
    arguments: list[tuple[tuple[object, ...], dict[str, object]]] = field(default_factory=list)
    valid: set[object] = field(default_factory=set)
    events: list[Callable[[], None]] = field(default_factory=list)

    def drain(self) -> None:
        """Run deferred GDB events in order."""
        while self.events:
            self.events.pop(0)()


@pytest.fixture
def breakpoints(fake_gdb: ModuleType, monkeypatch: pytest.MonkeyPatch) -> BreakpointHarness:
    """Add lifecycle behavior to the minimal fake GDB breakpoint classes."""
    harness = BreakpointHarness()

    def initialize(breakpoint: object, *args: object, **kwargs: object) -> None:
        harness.created.append(breakpoint)
        harness.arguments.append((args, kwargs))
        harness.valid.add(breakpoint)

    monkeypatch.setattr(fake_gdb.Breakpoint, "__init__", initialize)
    monkeypatch.setattr(
        fake_gdb.Breakpoint,
        "is_valid",
        lambda breakpoint: breakpoint in harness.valid,
        raising=False,
    )
    monkeypatch.setattr(
        fake_gdb.Breakpoint,
        "delete",
        lambda breakpoint: harness.valid.discard(breakpoint),
        raising=False,
    )
    monkeypatch.setattr(fake_gdb, "post_event", harness.events.append, raising=False)
    monkeypatch.setattr(fake_gdb, "newest_frame", Mock(return_value=object()), raising=False)
    return harness


@pytest.fixture
def camelot_project(tmp_path: Path) -> camelot.CamelotProject:
    """Provide cached metadata for a Camelot election trace."""
    metadata = camelot.TaskList(0x8001000, 0, 32, ())
    return camelot.CamelotProject(tmp_path, metadata)


def test_camelot_trace_collects_exact_limit_on_return(
    camelot_project: camelot.CamelotProject,
    breakpoints: BreakpointHarness,
    fake_gdb: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Camelot keeps return-value sampling, register fallback and chart semantics."""
    output = StringIO()
    on_delete = Mock()
    elected = Mock(side_effect=["worker", "idle"])
    monkeypatch.setattr(camelot, "elected_task", elected)
    fallback = Mock(return_value=43)
    monkeypatch.setattr(fake_gdb, "parse_and_eval", fallback, raising=False)
    trace = camelot_project.trace_scheduler(2, Console(file=output), on_delete)
    entry = breakpoints.created[0]
    assert trace is entry
    assert breakpoints.arguments[0] == (("sched_elect",), {"internal": True})
    assert entry.stop() is False
    first = breakpoints.created[-1]
    assert breakpoints.arguments[1] == ((fake_gdb.newest_frame.return_value,), {"internal": True})
    first.return_value = 42
    assert trace.elections == []
    assert first.stop() is False
    fallback.assert_not_called()
    assert trace.elections == ["worker"]
    assert entry.stop() is False
    second = breakpoints.created[-1]
    second.return_value = None
    assert second.stop() is True
    fallback.assert_called_once_with("$r0")
    assert [call.args[1] for call in elected.call_args_list] == [42, 43]
    assert all(call.args[0] is camelot_project.task_list for call in elected.call_args_list)
    assert trace.elections == ["worker", "idle"]
    assert "Task scheduling: elections 1-2" in " ".join(output.getvalue().split())
    assert entry.stop() is False
    assert len(breakpoints.created) == 3
    on_delete.assert_not_called()
    breakpoints.drain()
    on_delete.assert_called_once_with(trace)
    assert entry not in breakpoints.valid


def test_camelot_trace_cancels_pending_returns(
    camelot_project: camelot.CamelotProject,
    breakpoints: BreakpointHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelled or replaced traces cannot leave return breakpoints or record more tasks."""
    elected = Mock(side_effect=AssertionError("must not sample a cancelled trace"))
    monkeypatch.setattr(camelot, "elected_task", elected)
    on_delete = Mock()
    trace = camelot_project.trace_scheduler(2, Console(file=StringIO()), on_delete)
    entry = breakpoints.created[0]
    entry.stop()
    pending = breakpoints.created[-1]
    trace.delete()
    trace.delete()
    assert not breakpoints.valid
    assert pending.stop() is False
    assert entry.stop() is False
    assert trace.elections == []
    elected.assert_not_called()
    on_delete.assert_called_once_with(trace)


@pytest.mark.parametrize("failure", ["finish", "sample", "scope"])
def test_camelot_trace_failures_report_and_release_breakpoints(
    failure: str,
    camelot_project: camelot.CamelotProject,
    breakpoints: BreakpointHarness,
    fake_gdb: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asynchronous scheduler failures stop tracing rather than producing success."""
    output = StringIO()
    on_delete = Mock()
    trace = camelot_project.trace_scheduler(2, Console(file=output), on_delete)
    entry = breakpoints.created[0]
    if failure == "finish":
        fake_gdb.newest_frame.side_effect = fake_gdb.error("no frame")
        assert entry.stop() is True
        expected = "Cannot trace scheduler return: no frame"
    else:
        entry.stop()
        pending = breakpoints.created[-1]
        pending.return_value = 42
        if failure == "sample":
            monkeypatch.setattr(camelot, "elected_task", Mock(side_effect=ValueError("bad task")))
            assert pending.stop() is True
            expected = "Cannot read elected task: bad task"
        else:
            pending.out_of_scope()
            expected = "Cannot trace scheduler return: frame left scope"
    breakpoints.drain()
    on_delete.assert_called_once_with(trace)
    assert entry not in breakpoints.valid
    assert trace.elections == []
    assert expected in output.getvalue()
