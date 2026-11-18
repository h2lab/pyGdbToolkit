# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""PendSV observations, exact budgets, image ownership and breakpoint cleanup."""

from collections.abc import Callable
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
import shutil
import site
import subprocess
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

from elftools.common.exceptions import ELFError
from elftools.elf.sections import SymbolTableSection
import pytest
from rich.console import Console

from pyGdbToolkit.cmd_rtos import RtosCmd, rtos_status
from pyGdbToolkit.rtos import zephyr, zephyr_trace as trace
from pyGdbToolkit.rtos.base import RtosCapabilities
from pyGdbToolkit.shared.rtos.zephyr import ZephyrBuild

CONFIG = {"CONFIG_CPU_CORTEX_M": "y", "CONFIG_MP_MAX_NUM_CPUS": "1"}
IMAGE = Path("/build/zephyr/zephyr.elf")


def _value(fields: dict[str, object]) -> Mock:
    value = Mock()
    value.__getitem__ = Mock(side_effect=fields.__getitem__)
    return value


def _pointer(address: int) -> Mock:
    value = Mock()
    value.__int__ = Mock(return_value=address)
    value.type.target.return_value.sizeof = 144
    return value


@dataclass
class Harness:
    """Keep observable breakpoint and event-registry resources."""

    created: list[object] = field(default_factory=list)
    valid: set[object] = field(default_factory=set)
    deferred: list[Callable[[], None]] = field(default_factory=list)
    hooks: dict[str, list[Callable[..., None]]] = field(
        default_factory=lambda: {
            name: [] for name in ("exited", "clear_objfiles", "free_objfile", "breakpoint_deleted")
        }
    )
    arguments: list[tuple[tuple[object, ...], dict[str, object]]] = field(default_factory=list)

    def drain(self) -> None:
        """Dispatch deferred GDB-safe cleanup."""
        while self.deferred:
            self.deferred.pop(0)()


@pytest.fixture
def target(fake_gdb: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Harness:
    """Use exact pointers and fake GDB event registries, not a scheduler-name lookup."""
    harness = Harness()
    stopped = Mock()
    stopped.is_stopped.return_value = True
    inferior = Mock()
    inferior.threads.return_value = (stopped,)
    monkeypatch.setattr(fake_gdb, "selected_inferior", Mock(return_value=inferior))
    cpus = _value({0: _value({"current": _pointer(0x20000000)})})
    cpus.type.strip_typedefs.return_value.range.return_value = (0, 0)
    kernel = _value({"cpus": cpus, "ready_q": _value({"cache": _pointer(0x20000100)})})
    symbol = Mock()
    symbol.value.return_value = kernel
    objfile = Mock(filename=str(IMAGE))
    objfile.lookup_global_symbol.return_value = symbol
    monkeypatch.setattr(fake_gdb, "objfiles", Mock(return_value=[objfile]), raising=False)
    monkeypatch.setattr(trace, "_pendsv_address", Mock(return_value=0x08001000))
    monkeypatch.setattr(
        fake_gdb,
        "events",
        SimpleNamespace(
            **{
                name: SimpleNamespace(connect=callbacks.append, disconnect=callbacks.remove)
                for name, callbacks in harness.hooks.items()
            }
        ),
        raising=False,
    )

    def initialize(breakpoint: object, *args: object, **kwargs: object) -> None:
        harness.created.append(breakpoint)
        harness.valid.add(breakpoint)
        harness.arguments.append((args, kwargs))

    monkeypatch.setattr(fake_gdb.Breakpoint, "__init__", initialize)
    monkeypatch.setattr(
        fake_gdb.Breakpoint, "is_valid", lambda item: item in harness.valid, raising=False
    )
    deleting: set[object] = set()

    def delete(breakpoint: object) -> None:
        assert breakpoint not in deleting, "GDB deletion callback must not re-delete its breakpoint"
        deleting.add(breakpoint)
        for callback in tuple(harness.hooks["breakpoint_deleted"]):
            callback(breakpoint)
        harness.valid.discard(breakpoint)
        deleting.remove(breakpoint)

    monkeypatch.setattr(fake_gdb.Breakpoint, "delete", delete, raising=False)
    monkeypatch.setattr(fake_gdb, "post_event", harness.deferred.append, raising=False)
    monkeypatch.setattr(
        fake_gdb,
        "parse_and_eval",
        Mock(side_effect=AssertionError("no expressions")),
        raising=False,
    )
    monkeypatch.setattr(
        fake_gdb, "execute", Mock(side_effect=AssertionError("no execution changes")), raising=False
    )
    return harness


def _project(config: dict[str, str] = CONFIG, version: str = "4.5.99") -> zephyr.ZephyrProject:
    return zephyr.ZephyrProject(
        ZephyrBuild("app", "/app", "/zephyr", version, "board", ""), config, (), IMAGE
    )


def _kernel(fake_gdb: ModuleType) -> Mock:
    return fake_gdb.objfiles.return_value[0].lookup_global_symbol.return_value.value.return_value


def test_exact_entry_observations_preserve_duplicates_and_stop_before_switch(
    target: Harness, fake_gdb: ModuleType
) -> None:
    """No entry-return heuristic, task-list traversal or architecture register fallback."""
    output = StringIO()
    deleted = Mock()
    observation = _project().trace_scheduler(2, Console(file=output, width=160), deleted)
    assert _project().capabilities == RtosCapabilities(True, True, True, True)
    assert target.arguments == [(("*0x8001000",), {"internal": True})]
    assert observation.elections == []
    assert observation.stop() is False
    assert observation.elections == ["0x20000000 -> 0x20000100"]
    assert observation.stop() is True
    assert observation.elections == ["0x20000000 -> 0x20000100"] * 2
    assert "before switch" in " ".join(output.getvalue().split())
    assert "Not confirmed execution" in " ".join(output.getvalue().split())
    assert observation.stop() is False
    deleted.assert_not_called()
    assert target.valid
    target.drain()
    assert not target.valid and not any(target.hooks.values())
    deleted.assert_called_once_with(observation)
    observation.delete()
    deleted.assert_called_once()
    fake_gdb.parse_and_eval.assert_not_called()
    fake_gdb.execute.assert_not_called()


@pytest.mark.parametrize("limit", [0, -1, 4097, 10**30])
def test_invalid_limit_does_not_create_resources(target: Harness, limit: int) -> None:
    """Reject unbounded memory/output requests before any breakpoint is allocated."""
    with pytest.raises(ValueError, match="1..4096"):
        _project().trace_scheduler(limit, Console(file=StringIO()), Mock())
    assert not target.created and not any(target.hooks.values())


def test_exact_4096_observation_budget(target: Harness) -> None:
    """Exercise the real upper bound, not a monkeypatched smaller constant."""
    observation = _project().trace_scheduler(4096, Console(file=StringIO()), Mock())
    for _ in range(4095):
        assert observation.stop() is False
    assert observation.stop() is True
    assert len(observation.elections) == 4096
    assert observation.stop() is False
    target.drain()
    assert not target.valid


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"CONFIG_CPU_CORTEX_M": "y"},
        {**CONFIG, "CONFIG_MP_MAX_NUM_CPUS": "2"},
        {**CONFIG, "CONFIG_CPU_CORTEX_M": "n"},
        *(
            {**CONFIG, key: "y"}
            for key in (
                "CONFIG_SMP",
                "CONFIG_USE_SWITCH",
                "CONFIG_TRUSTED_EXECUTION_SECURE",
                "CONFIG_TRUSTED_EXECUTION_NONSECURE",
                "CONFIG_ARM_SECURE_FIRMWARE",
            )
        ),
    ],
)
def test_unsupported_layouts_and_versions_stay_gated(
    target: Harness, config: dict[str, str]
) -> None:
    """Eligibility uses cached Kconfig; missing CPU mapping is not silently single-CPU."""
    project = _project(config)
    assert not project.capabilities.scheduler_trace
    with pytest.raises(ValueError, match="one configured CPU"):
        project.trace_scheduler(1, Console(file=StringIO()), Mock())
    assert not target.created


def test_old_version_and_missing_symbols_create_no_breakpoints(
    target: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct API calls also reject unsupported ABI and absent hook symbols."""
    project = _project(version="4.0.0")
    assert not project.capabilities.scheduler_trace
    with pytest.raises(ValueError, match="4.1 through 4.x"):
        project.trace_scheduler(1, Console(file=StringIO()), Mock())
    monkeypatch.setattr(trace, "_pendsv_address", Mock(side_effect=ValueError("missing handler")))
    with pytest.raises(ValueError, match="missing handler"):
        _project().trace_scheduler(1, Console(file=StringIO()), Mock())
    assert not target.created and not any(target.hooks.values())


@pytest.mark.parametrize(
    "failure",
    [
        "no-table",
        "missing",
        "duplicate",
        "undefined",
        "not-function",
        "non-executable",
        "zero",
        "outside",
        "overflow",
        "malformed",
    ],
)
def test_invalid_elf_hook_metadata_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """A same-named data symbol, undefined function or malformed section cannot be a hook."""
    image = tmp_path / "image.elf"
    image.write_bytes(b"fixture")
    symbol = {"st_info": {"type": "STT_FUNC"}, "st_shndx": 1, "st_value": 0x08001001}
    if failure == "undefined":
        symbol["st_shndx"] = "SHN_UNDEF"
    elif failure == "not-function":
        symbol["st_info"] = {"type": "STT_OBJECT"}
    elif failure in {"zero", "outside", "overflow"}:
        symbol["st_value"] = {"zero": 0, "outside": 0x8002001, "overflow": 2**32 + 1}[failure]
    symbols = Mock(spec=SymbolTableSection)
    symbols.get_symbol_by_name.return_value = (
        None if failure == "missing" else [symbol, symbol] if failure == "duplicate" else [symbol]
    )
    elf = Mock()
    elf.get_section_by_name.return_value = None if failure == "no-table" else symbols
    elf.get_section.return_value = {
        "sh_addr": 0x08001000,
        "sh_size": 0x100,
        "sh_flags": 0 if failure == "non-executable" else 6,
    }
    monkeypatch.setattr(
        trace,
        "ELFFile",
        (
            Mock(side_effect=ELFError("truncated"))
            if failure == "malformed"
            else Mock(return_value=elf)
        ),
    )
    with pytest.raises(ValueError):
        trace._pendsv_address(image)


def test_exact_thumb_entry_comes_from_image_not_gdb_name_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Thumb marker is removed without using a prologue-skipping named breakpoint."""
    image = tmp_path / "image.elf"
    image.write_bytes(b"fixture")
    symbols = Mock(spec=SymbolTableSection)
    symbols.get_symbol_by_name.return_value = [
        {"st_info": {"type": "STT_FUNC"}, "st_shndx": 1, "st_value": 0x08001001}
    ]
    elf = Mock()
    elf.get_section_by_name.return_value = symbols
    elf.get_section.return_value = {"sh_addr": 0x08001000, "sh_size": 64, "sh_flags": 6}
    monkeypatch.setattr(trace, "ELFFile", Mock(return_value=elf))
    assert trace._pendsv_address(image) == 0x08001000


def test_missing_image_is_an_explicit_activation_error(tmp_path: Path) -> None:
    """Files removed after project loading fail through the standard ValueError boundary."""
    with pytest.raises(ValueError, match="Cannot decode Zephyr scheduler symbol"):
        trace._pendsv_address(tmp_path / "removed.elf")


def test_current_equal_to_candidate_is_still_one_observation(
    target: Harness, fake_gdb: ModuleType
) -> None:
    """A PendSV entry is counted even when it does not imply a thread identity change."""
    _kernel(fake_gdb)["ready_q"].__getitem__.side_effect = {
        "cache": _pointer(0x20000000)
    }.__getitem__
    observation = _project().trace_scheduler(1, Console(file=StringIO()), Mock())
    assert observation.stop() is True
    assert observation.elections == ["0x20000000 -> 0x20000000"]
    target.drain()


@pytest.mark.parametrize("stage", ["activation", "observation"])
@pytest.mark.parametrize("address", [0, 0x20000001, 0x100000000, 0xFFFFFFFC])
def test_invalid_thread_pointers_fail_explicitly(
    target: Harness, fake_gdb: ModuleType, stage: str, address: int
) -> None:
    """Do not append success-shaped records when current/candidate is invalid."""
    kernel = _kernel(fake_gdb)
    output = StringIO()
    deleted = Mock()
    if stage == "observation":
        observation = _project().trace_scheduler(2, Console(file=output), deleted)
    kernel["ready_q"].__getitem__.side_effect = {"cache": _pointer(address)}.__getitem__
    if stage == "activation":
        with pytest.raises(ValueError, match="scheduler thread address"):
            _project().trace_scheduler(2, Console(file=output), deleted)
        assert not target.created
    else:
        assert observation.stop() is True
        assert not observation.elections
        assert "Cannot observe Zephyr scheduler" in output.getvalue()
        target.drain()
        assert not target.valid
        deleted.assert_called_once_with(observation)


def test_runtime_read_error_stops_and_releases_trace(target: Harness, fake_gdb: ModuleType) -> None:
    """An asynchronous read failure is visible and stops execution instead of skipping a sample."""
    output = StringIO()
    deleted = Mock()
    observation = _project().trace_scheduler(2, Console(file=output), deleted)
    assert observation.stop() is False
    _kernel(fake_gdb).__getitem__.side_effect = fake_gdb.MemoryError("denied")
    assert observation.stop() is True
    assert len(observation.elections) == 1
    assert "denied" in output.getvalue()
    target.drain()
    deleted.assert_called_once_with(observation)
    assert not target.valid and not any(target.hooks.values())


@pytest.mark.parametrize("reason", ["running", "cpu-slots", "unloaded-image"])
def test_activation_preconditions_fail_without_resources(
    target: Harness, fake_gdb: ModuleType, reason: str
) -> None:
    """Require a stopped target, matching symbols and a single actual CPU slot."""
    if reason == "running":
        fake_gdb.selected_inferior.return_value.threads.return_value[0].is_stopped.return_value = (
            False
        )
    elif reason == "cpu-slots":
        _kernel(fake_gdb)["cpus"].type.strip_typedefs.return_value.range.return_value = (0, 1)
    else:
        fake_gdb.objfiles.return_value = []
    with pytest.raises(ValueError):
        _project().trace_scheduler(2, Console(file=StringIO()), Mock())
    assert not target.created and not any(target.hooks.values())


def test_names_are_optional_bounded_and_not_markup(
    target: Harness, fake_gdb: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stable addresses remain the identity even when names repeat or contain Rich markup."""
    name = Mock(return_value="[red]worker[/red]")
    monkeypatch.setattr(trace, "thread_name", name)
    output = StringIO()
    observation = _project({**CONFIG, "CONFIG_THREAD_NAME": "y"}).trace_scheduler(
        1, Console(file=output, width=220, color_system=None), Mock()
    )
    assert observation.stop() is True
    assert "[red]worker[/red]" in output.getvalue()
    assert "0x20000000" in observation.elections[0]
    assert "0x20000100" in observation.elections[0]
    target.drain()


@pytest.mark.parametrize(
    "event", ["exited", "clear_objfiles", "free_objfile", "breakpoint_deleted"]
)
def test_target_image_and_external_breakpoint_deletion_cancel_trace(
    target: Harness, fake_gdb: ModuleType, event: str
) -> None:
    """The session cannot retain an apparently active trace after losing its observation point."""
    output = StringIO()
    deleted = Mock()
    observation = _project().trace_scheduler(3, Console(file=output), deleted)
    callback = target.hooks[event][0]
    payload = (
        observation
        if event == "breakpoint_deleted"
        else SimpleNamespace(objfile=SimpleNamespace(filename=str(IMAGE)))
    )
    if event == "breakpoint_deleted":
        fake_gdb.Breakpoint.delete(observation)
    else:
        callback(payload)
    assert "cancelled" in output.getvalue()
    assert observation.stop() is False
    assert not target.valid and not any(target.hooks.values())
    deleted.assert_called_once_with(observation)


def test_unrelated_objfiles_and_breakpoints_do_not_cancel(target: Harness) -> None:
    """Only the project's image and owned breakpoint control this trace."""
    observation = _project().trace_scheduler(1, Console(file=StringIO()), Mock())
    target.hooks["free_objfile"][0](SimpleNamespace(objfile=SimpleNamespace(filename="/other.elf")))
    target.hooks["breakpoint_deleted"][0](object())
    assert observation.active
    observation.delete()
    assert not target.valid and not any(target.hooks.values())


def test_generic_session_status_replacement_and_reset_own_trace_resources(
    target: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    """No new RPC schema or architecture branches are required for Zephyr observations."""
    command = RtosCmd()
    command.state.selected_rtos = zephyr.ZephyrRtos()
    command.state.project = _project()
    command.state.project_path = Path("/build")
    command._invoke_showsched(["2"])
    first = command.state.scheduler_trace
    assert isinstance(first, trace.PendSvTrace)
    assert first.stop() is False
    assert rtos_status()["scheduler_trace"] == {
        "limit": 2,
        "elections": ["0x20000000 -> 0x20000100"],
    }
    command._invoke_showsched(["1"])
    second = command.state.scheduler_trace
    assert first is not second and not first.active
    first.delete()
    assert command.state.scheduler_trace is second
    assert second.stop() is True
    target.drain()
    assert rtos_status()["scheduler_trace"] is None
    command._invoke_showsched(["3"])
    command.state.reset()
    assert not target.valid and not any(target.hooks.values())
    assert "Stopped before PendSV" in capsys.readouterr().out


@pytest.mark.skipif(
    shutil.which("gdb-multiarch") is None or shutil.which("cc") is None,
    reason="gdb-multiarch and a C compiler are needed",
)
def test_real_gdb_exact_entry_and_event_lifecycle(tmp_path: Path) -> None:
    """A native C surrogate verifies GDB breakpoint/event behavior, not Arm exception execution."""
    source = tmp_path / "scheduler.c"
    source.write_text("""
struct k_thread { int marker; };
struct k_thread a={1}, b={2};
struct z_kernel {
    struct { struct k_thread *current; } cpus[1];
    struct { struct k_thread *cache; } ready_q;
} _kernel = {{{&a}}, {&b}};
__attribute__((noinline,aligned(4))) void z_arm_pendsv(void) {
    struct k_thread *old = _kernel.cpus[0].current;
    _kernel.cpus[0].current = _kernel.ready_q.cache;
    _kernel.ready_q.cache = old;
}
int main(void) {
    for (int i=0; i<5; ++i) z_arm_pendsv();
    return 0;
}
""")
    image = tmp_path / "scheduler"
    subprocess.run(["cc", "-g", "-O0", "-no-pie", str(source), "-o", str(image)], check=True)
    paths = [str(Path(__file__).resolve().parents[1] / "src"), *site.getsitepackages()]
    setup = (
        f"import sys; sys.path[:0]={paths!r}; "
        "from pathlib import Path; import pyGdbToolkit; "
        "from pyGdbToolkit.cmd_rtos import RtosCmd; "
        "from pyGdbToolkit.rtos.zephyr import ZephyrProject, ZephyrRtos; "
        "from pyGdbToolkit.shared.rtos.zephyr import ZephyrBuild; "
        "command=RtosCmd(); command.state.selected_rtos=ZephyrRtos(); "
        "build=ZephyrBuild('app','/app','/zephyr','4.5.99','board',''); "
        f"command.state.project=ZephyrProject(build,{CONFIG!r},(),Path({str(image)!r})); "
        "command._invoke_showsched(['3']); observation=command.state.scheduler_trace; "
        "a=int(gdb.lookup_global_symbol('a').value().address); "
        "b=int(gdb.lookup_global_symbol('b').value().address)"
    )
    verify = (
        "assert observation.elections == [f'0x{a:08X} -> 0x{b:08X}', "
        "f'0x{b:08X} -> 0x{a:08X}', f'0x{a:08X} -> 0x{b:08X}']; "
        "assert int(gdb.parse_and_eval('_kernel.cpus[0].current')) == a; "
        "observation.delete(); assert command.state.scheduler_trace is None; "
        "command._invoke_showsched(['2']); pending=command.state.scheduler_trace; "
        "gdb.Breakpoint.delete(pending); assert command.state.scheduler_trace is None; "
        "assert not pending.is_valid(); "
        "command._invoke_showsched(['2']); pending=command.state.scheduler_trace; "
        "gdb.execute('set confirm off'); gdb.execute('file'); "
        "assert command.state.scheduler_trace is None; "
        "assert not pending.is_valid(); print('ZEPHYR_TRACE_GDB_VERIFIED')"
    )
    result = subprocess.run(
        [
            "gdb-multiarch",
            "-nx",
            "-batch",
            str(image),
            "-ex",
            "start",
            "-ex",
            f"python {setup}",
            "-ex",
            "continue",
            "-ex",
            f"python {verify}",
        ],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ZEPHYR_TRACE_GDB_VERIFIED" in result.stdout, result.stdout + result.stderr
