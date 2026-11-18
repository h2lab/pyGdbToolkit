# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""DWARF-driven Zephyr thread sampling without a physical target."""

from io import StringIO
from pathlib import Path
import shutil
import site
import subprocess
from types import ModuleType
from unittest.mock import Mock

import pytest
from rich.console import Console

from pyGdbToolkit.cmd_rtos import RtosCmd
from pyGdbToolkit.rtos.base import RtosCapabilities
from pyGdbToolkit.rtos import zephyr, zephyr_threads as live
from pyGdbToolkit.shared.rtos.zephyr import ZephyrBuild

MONITOR = {"CONFIG_THREAD_MONITOR": "y", "CONFIG_THREAD_NAME": "y"}


def _value(fields: dict[str, object]) -> Mock:
    value = Mock()
    value.__getitem__ = Mock(side_effect=fields.__getitem__)
    return value


def _thread(
    *,
    name: str = "worker",
    priority: int = -2,
    state: int = 0x02,
    options: int = 0x04,
    next_thread: int = 0,
) -> Mock:
    name_value = Mock()
    kind = name_value.type.strip_typedefs.return_value
    kind.code = 1
    kind.target.return_value.sizeof = 1
    kind.range.return_value = (0, 15)
    name_value.string.return_value = name + "\0" + "unused"
    return _value(
        {
            "base": _value({"prio": priority, "thread_state": state, "user_options": options}),
            "name": name_value,
            "entry": _value({"pEntry": 0x08001001}),
            "next_thread": next_thread,
        }
    )


@pytest.fixture
def target(fake_gdb: ModuleType, monkeypatch: pytest.MonkeyPatch) -> dict[int, Mock]:
    """Install named GDB values, not expression parsing or fixed thread byte offsets."""
    stopped = Mock()
    stopped.is_stopped.return_value = True
    inferior = Mock()
    inferior.threads.return_value = (stopped,)
    monkeypatch.setattr(fake_gdb, "selected_inferior", Mock(return_value=inferior))
    monkeypatch.setattr(fake_gdb, "TYPE_CODE_ARRAY", 1, raising=False)
    cpus = _value({0: _value({"current": 0x20000000, "idle_thread": 0x20000100, "nested": 0})})
    cpus.type.strip_typedefs.return_value.range.return_value = (0, 0)
    kernel = _value({"cpus": cpus, "threads": 0x20000000})
    symbol = Mock()
    symbol.value.return_value = kernel
    monkeypatch.setattr(fake_gdb, "lookup_global_symbol", Mock(return_value=symbol), raising=False)
    objfile = Mock(filename="/build/zephyr/zephyr.elf")
    objfile.lookup_global_symbol.return_value = symbol
    monkeypatch.setattr(fake_gdb, "objfiles", Mock(return_value=[objfile]), raising=False)
    thread_type = Mock(sizeof=144)
    monkeypatch.setattr(fake_gdb, "lookup_type", Mock(return_value=thread_type), raising=False)
    threads = {
        0x20000000: _thread(next_thread=0x20000100),
        0x20000100: _thread(name="idle", priority=15, state=0x80, options=1),
    }

    def pointer(address: int) -> Mock:
        value = Mock()
        if address in threads:
            value.cast.return_value.dereference.return_value = threads[address]
        else:
            value.cast.return_value.dereference.side_effect = fake_gdb.MemoryError(
                f"unreadable 0x{address:X}"
            )
        return value

    monkeypatch.setattr(fake_gdb, "Value", Mock(side_effect=pointer), raising=False)
    return threads


def _render(config: dict[str, str], identifier: str | None = None) -> str:
    stream = StringIO()
    console = Console(file=stream, width=220, color_system=None)
    console.print(live.show_threads(config, "4.5.99", identifier))
    return stream.getvalue()


def test_monitor_list_decodes_named_threads_and_roles(target: dict[int, Mock]) -> None:
    """Linked-list traversal and field names, not ABI offsets, define the sample."""
    sample = live.read_threads(MONITOR)
    assert len(sample.threads) == 2
    worker, idle = sample.threads
    assert worker.address == 0x20000000
    assert worker.name == "worker"
    assert worker.priority == -2
    assert worker.state == 2
    assert worker.state_text == "pending"
    assert worker.options == 4
    assert worker.entry == 0x08001001
    assert worker.roles == ("current CPU 0",)
    assert idle.roles == ("idle_thread CPU 0",)
    assert idle.state_text == "queued"
    output = _render(MONITOR)
    assert "worker" in output and "idle" in output
    assert "CONFIG_THREAD_MONITOR=y" in output
    assert "0x20000000" in output
    details = _render(MONITOR, "worker")
    assert "0x0004" in details
    assert "0x08001001" in details
    assert "halted sample" in output


def test_disabled_monitor_and_names_are_explicit_partial_coverage(
    target: dict[int, Mock], fake_gdb: ModuleType
) -> None:
    """Production builds still expose current/idle without pretending these are all threads."""
    kernel = fake_gdb.lookup_global_symbol.return_value.value.return_value
    kernel.__getitem__.side_effect = {"cpus": kernel["cpus"]}.__getitem__
    for thread in target.values():
        base = thread["base"]
        thread.__getitem__.side_effect = {"base": base}.__getitem__
    sample = live.read_threads({})
    assert len(sample.threads) == 2
    assert all(thread.name is None and thread.entry is None for thread in sample.threads)
    output = _render({})
    assert "Incomplete enumeration" in output
    assert "Thread names unavailable" in output
    assert "<unavailable>" in output
    assert "no monitor" in _render({}, "0x20000000")
    with pytest.raises(ValueError, match="names unavailable"):
        live.show_threads({}, "4.5.99", "worker")


def test_duplicate_names_require_address_selection(target: dict[int, Mock]) -> None:
    """Thread names are not unique handles; addresses disambiguate."""
    target[0x20000100] = _thread()
    with pytest.raises(ValueError, match="Ambiguous.*0x20000000, 0x20000100"):
        live.show_threads(MONITOR, "4.5.99", "worker")
    output = _render(MONITOR, "0x20000100")
    assert "0x20000100" in output
    assert "0x20000000" not in output


def test_explicit_address_does_not_evaluate_gdb_expressions(
    target: dict[int, Mock], fake_gdb: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An address can name an unmonitored dynamic thread; expressions are never executed."""
    evaluate = Mock(side_effect=AssertionError("must not evaluate"))
    monkeypatch.setattr(fake_gdb, "parse_and_eval", evaluate, raising=False)
    target[0x20000200] = _thread(name="extra")
    output = _render({}, "0x20000200")
    assert "membership is not verified" in output
    assert "0x20000200" in output
    with pytest.raises(ValueError, match="Unknown Zephyr thread"):
        live.show_threads(MONITOR, "4.5.99", "worker; set $pc=0")
    evaluate.assert_not_called()


@pytest.mark.parametrize("identifier", ["0x0", "0x20000001", "0x100000000", "0xFFFFFFFC"])
def test_invalid_addresses_fail_explicitly(target: dict[int, Mock], identifier: str) -> None:
    """Null, misaligned and overflowing addresses never become valid-looking threads."""
    with pytest.raises(ValueError, match="Invalid Zephyr k_thread address"):
        live.show_threads(MONITOR, "4.5.99", identifier)


@pytest.mark.parametrize("next_address", [0x20000000, 0x20000100])
def test_monitor_cycles_are_errors_not_truncated_success(
    target: dict[int, Mock], next_address: int
) -> None:
    """Both head cycles and cycles later in the list are detected."""
    target[0x20000100] = _thread(next_thread=next_address)
    with pytest.raises(ValueError, match="Cycle in Zephyr"):
        live.read_threads(MONITOR)


def test_monitor_size_limit_is_enforced(
    target: dict[int, Mock], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt or oversized chain has a strict traversal budget."""
    monkeypatch.setattr(live, "MAX_THREADS", 1)
    with pytest.raises(ValueError, match="exceeds 1 entries"):
        live.read_threads(MONITOR)


@pytest.mark.parametrize("count", [4096, 4097])
def test_actual_monitor_budget_boundary(
    target: dict[int, Mock], fake_gdb: ModuleType, count: int
) -> None:
    """Exactly 4096 monitored entries work; a 4097th is rejected before dereferencing it."""
    template = _thread()
    common = {key: template[key] for key in ("base", "name", "entry")}

    def pointer(address: int) -> Mock:
        index = (address - 0x20000000) // 0x100
        value = Mock()
        value.cast.return_value.dereference.return_value = {
            **common,
            "next_thread": address + 0x100 if index + 1 < count else 0,
        }
        return value

    fake_gdb.Value.side_effect = pointer
    if count == 4096:
        assert len(live.read_threads(MONITOR).threads) == 4096
    else:
        with pytest.raises(ValueError, match="exceeds 4096 entries"):
            live.read_threads(MONITOR)
    assert fake_gdb.Value.call_count == 4096


def test_unreadable_thread_fails_the_sample(target: dict[int, Mock]) -> None:
    """A read failure cannot be hidden behind a partial healthy thread list."""
    target[0x20000100] = _thread(next_thread=0x20000300)
    with pytest.raises(ValueError, match="Cannot read Zephyr.*unreadable"):
        live.read_threads(MONITOR)


@pytest.mark.parametrize("stopped", [False, None])
def test_running_or_disconnected_target_is_rejected_before_memory_reads(
    target: dict[int, Mock], fake_gdb: ModuleType, stopped: bool | None
) -> None:
    """Do not sample a running target or use ELF initializers as live memory."""
    thread = Mock()
    thread.is_stopped.return_value = stopped
    fake_gdb.selected_inferior.return_value.threads.return_value = (
        () if stopped is None else (thread,)
    )
    with pytest.raises(ValueError, match="stop all"):
        live.read_threads(MONITOR)
    fake_gdb.lookup_global_symbol.assert_not_called()


def test_missing_dwarf_symbol_is_explicit(target: dict[int, Mock], fake_gdb: ModuleType) -> None:
    """Missing required DWARF is a failure, not an empty thread list."""
    fake_gdb.lookup_global_symbol.return_value = None
    with pytest.raises(ValueError, match="missing _kernel symbol"):
        live.read_threads(MONITOR)


def test_project_sampling_uses_its_own_objfile_and_compilation_unit(
    target: dict[int, Mock], fake_gdb: ModuleType
) -> None:
    """Previously loaded images cannot silently supply another build's kernel or thread type."""
    image = Path("/build/zephyr/zephyr.elf")
    sample = live.read_threads(MONITOR, image=image)
    assert len(sample.threads) == 2
    fake_gdb.lookup_global_symbol.assert_not_called()
    objfile = fake_gdb.objfiles.return_value[0]
    objfile.lookup_global_symbol.assert_called_once_with("_kernel")
    symbol = objfile.lookup_global_symbol.return_value
    fake_gdb.lookup_type.assert_called_once_with("struct k_thread", symbol.symtab.global_block())
    with pytest.raises(ValueError, match="symbol file is no longer loaded"):
        live.read_threads(MONITOR, image=Path("/other/zephyr.elf"))


def test_empty_monitor_and_null_cpu_threads_describe_boot_state(
    target: dict[int, Mock], fake_gdb: ModuleType
) -> None:
    """Reset/early boot zeros must not look like a healthy kernel with no threads."""
    kernel = fake_gdb.lookup_global_symbol.return_value.value.return_value
    cpus = kernel["cpus"]
    cpus.__getitem__.side_effect = {
        0: _value({"current": 0, "idle_thread": 0, "nested": 0})
    }.__getitem__
    kernel.__getitem__.side_effect = {"cpus": cpus, "threads": 0}.__getitem__
    sample = live.read_threads(MONITOR)
    assert not sample.threads
    assert len([note for note in sample.notes if "kernel may not be initialized" in note]) == 2


def test_missing_monitor_members_and_irq_context_are_labeled(
    target: dict[int, Mock], fake_gdb: ModuleType
) -> None:
    """CPU references supplement monitoring without hiding a possibly transitional snapshot."""
    kernel = fake_gdb.lookup_global_symbol.return_value.value.return_value
    cpus = kernel["cpus"]
    cpus.__getitem__.side_effect = {
        0: _value({"current": 0x20000000, "idle_thread": 0x20000100, "nested": 2})
    }.__getitem__
    kernel.__getitem__.side_effect = {"cpus": cpus, "threads": 0}.__getitem__
    sample = live.read_threads(MONITOR)
    assert "IRQ nesting 2" in sample.threads[0].roles[0]
    assert len([note for note in sample.notes if "absent from monitor" in note]) == 2


@pytest.mark.parametrize(
    "bits,expected",
    [
        (0, "ready"),
        (0x01, "dummy"),
        (0x06, "pending | sleeping"),
        (0x18, "dead | suspended"),
        (0x60, "aborting | suspending"),
        (0x80, "queued"),
    ],
)
def test_state_flags_are_not_collapsed_to_a_single_state(bits: int, expected: str) -> None:
    """Preserve combinations and keep scheduled/current identity separate."""
    assert live.ThreadInfo(0x20000000, None, 0, bits, 0, (), None).state_text == expected


def test_name_reads_are_bounded_and_rich_markup_is_literal(target: dict[int, Mock]) -> None:
    """Read only the DWARF array length and never interpret firmware strings as markup."""
    target[0x20000000] = _thread(name="[red]worker[/red]")
    output = _render(MONITOR)
    assert "[red]worker[/red]" in output
    target[0x20000000]["name"].string.assert_called_once_with(
        encoding="utf-8", errors="replace", length=16
    )


def test_invalid_name_array_is_rejected(target: dict[int, Mock]) -> None:
    """Malformed DWARF cannot trigger an unbounded string read."""
    value = target[0x20000000]["name"]
    value.type.strip_typedefs.return_value.range.return_value = (0, 4096)
    with pytest.raises(ValueError, match="invalid k_thread.name size"):
        live.read_threads(MONITOR)
    value.string.assert_not_called()


def test_generic_commands_dispatch_zephyr_threads_without_camelot_assumptions(
    target: dict[int, Mock], capsys: pytest.CaptureFixture[str]
) -> None:
    """Both public commands use the implementation, with a static snapshot still absent."""
    project = zephyr.ZephyrProject(
        ZephyrBuild("app", "/app", "/zephyr", "4.5.99", "board", ""),
        MONITOR,
        (),
        Path("/build/zephyr/zephyr.elf"),
    )
    command = RtosCmd()
    command.state.selected_rtos = zephyr.ZephyrRtos()
    command.state.project = project
    command.state.project_path = Path("/build")
    command._invoke_show_threads([])
    command._invoke_show_task(["worker"])
    assert "worker" in capsys.readouterr().out
    assert project.task_list_snapshot() is None
    assert project.capabilities == RtosCapabilities(True, False, True)


def test_thread_command_reports_usage_and_missing_project(fake_gdb: ModuleType) -> None:
    """New generic entrypoint preserves standard command error handling."""
    command = RtosCmd()
    with pytest.raises(fake_gdb.GdbError, match="Usage: rtos show threads"):
        command._invoke_show_threads(["extra"])
    with pytest.raises(fake_gdb.GdbError, match="Select an RTOS and load"):
        command._invoke_show_threads([])


@pytest.mark.parametrize(
    "version,supported",
    [
        ("3.7.0", False),
        ("4.0.0", False),
        ("4.1.0", True),
        ("4.5.99", True),
        ("4.2.0-rc1", True),
        ("5.0.0", False),
        ("unknown", False),
    ],
)
def test_live_capabilities_follow_the_supported_state_abi(version: str, supported: bool) -> None:
    """Do not label bit 2 as sleeping for kernels preceding its introduction in 4.1."""
    project = zephyr.ZephyrProject(
        ZephyrBuild("app", "/app", "/zephyr", version, "board", ""),
        MONITOR,
        (),
        Path("/build/zephyr/zephyr.elf"),
    )
    assert project.capabilities == RtosCapabilities(supported, False, supported)
    if not supported:
        with pytest.raises(ValueError, match="requires version 4.1 through 4.x"):
            project.show_threads()
        with pytest.raises(ValueError, match="requires version 4.1 through 4.x"):
            project.show_task("worker")


@pytest.mark.skipif(
    shutil.which("gdb-multiarch") is None or shutil.which("cc") is None,
    reason="gdb-multiarch and a C compiler are needed",
)
def test_monitor_fields_with_real_gdb_dwarf_and_memory(tmp_path: Path) -> None:
    """Validate GDB Value semantics using a local process, not hardware or mocked offsets."""
    source = tmp_path / "threads.c"
    source.write_text("""
struct _thread_base {
    unsigned char padding[13];
    signed char prio;
    unsigned char thread_state;
    unsigned short user_options;
};
struct k_thread {
    struct _thread_base base;
    char name[16];
    struct { void (*pEntry)(void); } entry;
    struct k_thread *next_thread;
};
struct _cpu {
    unsigned int nested;
    struct k_thread *current;
    struct k_thread *idle_thread;
};
struct z_kernel {
    struct _cpu cpus[1];
    struct k_thread *threads;
};
void entry(void) {}
struct k_thread idle = {
    .base = {.prio = 15, .thread_state = 0x80, .user_options = 1},
    .name = "idle", .entry = {entry}
};
struct k_thread worker = {
    .base = {.prio = -2, .thread_state = 0x06, .user_options = 4},
    .name = "worker", .entry = {entry}, .next_thread = &idle
};
struct z_kernel _kernel = {{ {0, &worker, &idle} }, &worker};
int main(void) { return 0; }
""")
    image = tmp_path / "threads"
    subprocess.run(["cc", "-g", "-O0", "-no-pie", str(source), "-o", str(image)], check=True)
    paths = [str(Path(__file__).resolve().parents[1] / "src"), *site.getsitepackages()]
    script = (
        f"import sys; sys.path[:0] = {paths!r}; "
        "from pathlib import Path; "
        "from pyGdbToolkit.rtos.zephyr_threads import read_threads; "
        f"s = read_threads({MONITOR!r}, image=Path({str(image)!r})); "
        "assert len(s.threads) == 2; w,i = s.threads; "
        "assert w.name == 'worker' and w.priority == -2 and w.state_text == 'pending | sleeping'; "
        "assert w.options == 4 and w.entry; "
        "assert i.name == 'idle' and i.roles == ('idle_thread CPU 0',); "
        "from pyGdbToolkit.session import SESSION; "
        "from pyGdbToolkit.cmd_rtos import RtosSessionState; "
        "from pyGdbToolkit.rtos.zephyr import ZephyrProject, ZephyrRtos; "
        "from pyGdbToolkit.shared.rtos.zephyr import ZephyrBuild; "
        "state=SESSION.state(RtosSessionState); state.selected_rtos=ZephyrRtos(); "
        "build=ZephyrBuild('app', '/app', '/zephyr', '4.5.99', 'board', ''); "
        f"state.project=ZephyrProject(build, {MONITOR!r}, (), Path({str(image)!r})); "
        "listing=gdb.execute('rtos show threads', to_string=True); "
        "assert 'Zephyr threads' in listing and f'0x{w.address:08X}' in listing; "
        "detail=gdb.execute('rtos show task worker', to_string=True); "
        "assert '0x0004' in detail and 'Thread details' in detail; "
        "print('ZEPHYR_DWARF_VERIFIED')"
    )
    result = subprocess.run(
        ["gdb-multiarch", "-nx", "-batch", str(image), "-ex", "start", "-ex", f"python {script}"],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ZEPHYR_DWARF_VERIFIED" in result.stdout, result.stdout + result.stderr
