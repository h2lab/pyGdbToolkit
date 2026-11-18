# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Bounded synchronization inspection, invalid memory and honest wait evidence."""

from io import StringIO
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest
from rich.console import Console, Group

from pyGdbToolkit.cmd_rtos import RtosCmd, RtosShowObjectCmd
from pyGdbToolkit.rtos import camelot, zephyr, zephyr_sync as sync
from pyGdbToolkit.shared.rtos.zephyr import ZephyrBuild

IMAGE = Path("/build/zephyr/zephyr.elf")
OBJECT = 0x21001000
QUEUE = OBJECT + 8
HEAD = QUEUE + 4
THREAD = 0x20000000
NODE = THREAD + 20
CONFIG = {"CONFIG_WAITQ_SIMPLE": "y", "CONFIG_EVENTS": "y"}


def _value(fields: dict[str, object], address: int = 0, size: int = 8) -> Mock:
    value = Mock()
    value.__getitem__ = Mock(side_effect=fields.__getitem__)
    value.address = address
    value.type.sizeof = size
    return value


def _render(group: Group) -> str:
    stream = StringIO()
    Console(file=stream, width=220, color_system=None).print(group)
    return stream.getvalue()


@pytest.fixture
def target(fake_gdb: ModuleType, monkeypatch: pytest.MonkeyPatch) -> dict[int, Mock]:
    """Model member addresses with nonzero offsets and no expression evaluator."""
    stopped = Mock()
    stopped.is_stopped.return_value = True
    inferior = Mock()
    inferior.threads.return_value = (stopped,)
    monkeypatch.setattr(fake_gdb, "selected_inferior", Mock(return_value=inferior))
    symbol = Mock()
    objfile = Mock(filename=str(IMAGE))
    objfile.lookup_global_symbol.return_value = symbol
    monkeypatch.setattr(fake_gdb, "objfiles", Mock(return_value=[objfile]), raising=False)
    object_type = Mock(sizeof=48, objfile=objfile)
    thread_type = Mock(sizeof=128, objfile=objfile)
    monkeypatch.setattr(
        fake_gdb,
        "lookup_type",
        Mock(
            side_effect=lambda name, block: (
                thread_type if name == "struct k_thread" else object_type
            )
        ),
        raising=False,
    )
    head = _value({"next": NODE, "prev": NODE}, HEAD)
    queue = _value({"waitq": head}, QUEUE, 12)
    memory = {
        0x1000: _value({"base": _value({"qnode_dlist": _value({}, 0x1014)})}),
        OBJECT: _value(
            {
                "wait_q": queue,
                "count": 0,
                "limit": 4,
                "owner": THREAD,
                "lock_count": 2,
                "events": 3,
            },
            OBJECT,
            48,
        ),
        NODE: _value({"next": HEAD, "prev": HEAD}, NODE),
        THREAD: _value(
            {
                "base": _value({"pended_on": QUEUE, "thread_state": 6, "prio": -2}),
                "events": 5,
                "event_options": 1,
            },
            THREAD,
            128,
        ),
    }

    def pointer(address: int) -> Mock:
        value = Mock()
        if address in memory:
            value.cast.return_value.dereference.return_value = memory[address]
        else:
            value.cast.return_value.dereference.side_effect = fake_gdb.MemoryError(
                f"unreadable 0x{address:X}"
            )
        return value

    monkeypatch.setattr(fake_gdb, "Value", Mock(side_effect=pointer))
    monkeypatch.setattr(
        fake_gdb,
        "parse_and_eval",
        Mock(side_effect=AssertionError("no expressions")),
        raising=False,
    )
    monkeypatch.setattr(
        fake_gdb, "execute", Mock(side_effect=AssertionError("no target changes")), raising=False
    )
    return memory


@pytest.mark.parametrize(
    "kind,expected",
    [("sem", "0 / 4"), ("mutex", "Recursive lock count"), ("event", "0x00000003")],
)
@pytest.mark.parametrize("waitq_key", ["CONFIG_WAITQ_SIMPLE", "CONFIG_WAITQ_DUMB"])
def test_typed_objects_follow_dwarf_node_offsets_and_queue_backlinks(
    target: dict[int, Mock], fake_gdb: ModuleType, kind: str, expected: str, waitq_key: str
) -> None:
    """The object queue, queue head and thread node are not assumed to be at offset zero."""
    config = {"CONFIG_EVENTS": "y", waitq_key: "y"}
    output = _render(sync.show_object(kind, hex(OBJECT), config, IMAGE))
    assert expected in output
    assert f"0x{THREAD:08X}" in output
    assert f"0x{QUEUE:08X}" in output
    assert "0x06" in output and "-2" in output
    assert "1 waiter(s)" in output
    assert "not a scheduling prediction" in " ".join(output.split())
    assert "lifetime are not verified" in output
    assert "deadlock proof" in output
    if kind == "event":
        assert "0x00000005" in output and "all / 0x00000001" in output
    else:
        assert "Requested events" not in output
    block = fake_gdb.objfiles.return_value[
        0
    ].lookup_global_symbol.return_value.symtab.global_block()
    assert all(call.args[1] is block for call in fake_gdb.lookup_type.call_args_list)
    fake_gdb.parse_and_eval.assert_not_called()
    fake_gdb.execute.assert_not_called()


def test_empty_queue_is_explicit_and_unlocked_mutex_has_no_owner(target: dict[int, Mock]) -> None:
    """A sentinel linked to itself represents exactly zero waiters."""
    queue = target[OBJECT]["wait_q"]
    queue.__getitem__.side_effect = {
        "waitq": _value({"next": HEAD, "prev": HEAD}, HEAD)
    }.__getitem__
    target[OBJECT].__getitem__.side_effect = {
        "wait_q": queue,
        "owner": 0,
        "lock_count": 0,
    }.__getitem__
    output = _render(sync.show_object("mutex", hex(OBJECT), CONFIG, IMAGE))
    assert "0 waiter(s)" in output and "none" in output


@pytest.mark.parametrize("options,mode", [(0, "any"), (1, "all"), (6, "any")])
def test_event_fields_only_interpreted_for_known_event_waiters(
    target: dict[int, Mock], options: int, mode: str
) -> None:
    """Event options other than the any/all bit remain visible as raw evidence."""
    thread = target[THREAD]
    thread.__getitem__.side_effect = {
        "base": thread["base"],
        "events": 0x80000000,
        "event_options": options,
    }.__getitem__
    output = _render(sync.show_object("event", hex(OBJECT), CONFIG, IMAGE))
    assert f"{mode} / 0x{options:08X}" in output and "0x80000000" in output


@pytest.mark.parametrize(
    "kind,address",
    [
        ("pipe", "0x20001000"),
        ("sem", "tokens"),
        ("sem", "0x20001000; continue"),
        ("sem", "0"),
        ("sem", "0x0"),
        ("sem", "0x20001001"),
        ("sem", "0x100000000"),
        ("sem", "0xFFFFFFFC"),
    ],
)
def test_invalid_inputs_never_become_gdb_expressions(
    target: dict[int, Mock], fake_gdb: ModuleType, kind: str, address: str
) -> None:
    """Reject arbitrary expressions, wrong types, nulls, alignment and complete-range overflow."""
    with pytest.raises(ValueError):
        sync.show_object(kind, address, CONFIG, IMAGE)
    fake_gdb.Value.assert_not_called()
    fake_gdb.parse_and_eval.assert_not_called()


@pytest.mark.parametrize(
    "fields,message",
    [
        ({"count": 5, "limit": 4}, "count/limit"),
        ({"count": 0, "limit": 0}, "count/limit"),
        ({"count": -1, "limit": 4}, "unsigned 32-bit"),
        ({"count": 0, "limit": 2**32}, "unsigned 32-bit"),
        ({"owner": THREAD, "lock_count": 0}, "owner/lock_count"),
        ({"owner": 0, "lock_count": 1}, "owner/lock_count"),
        ({"owner": THREAD + 1, "lock_count": 1}, "address"),
        ({"events": -1}, "unsigned 32-bit"),
    ],
)
def test_object_invariants_fail_instead_of_showing_healthy_state(
    target: dict[int, Mock], fields: dict[str, int], message: str
) -> None:
    """Corrupt or mid-update fields remain explicit failures."""
    target[OBJECT].__getitem__.side_effect = fields.__getitem__
    kind = "sem" if "count" in fields else "mutex" if "owner" in fields else "event"
    with pytest.raises(ValueError, match=message):
        sync.show_object(kind, hex(OBJECT), CONFIG, IMAGE)


@pytest.mark.parametrize(
    "fields,message",
    [
        ({"pended_on": QUEUE + 4, "thread_state": 2, "prio": 1}, "disagrees"),
        ({"pended_on": QUEUE, "thread_state": 0, "prio": 1}, "non-pending"),
        ({"pended_on": QUEUE, "thread_state": 10, "prio": 1}, "terminated"),
        ({"pended_on": QUEUE, "thread_state": 258, "prio": 1}, "non-pending"),
        ({"pended_on": QUEUE, "thread_state": 2, "prio": 128}, "priority"),
    ],
)
def test_waiter_state_and_backlink_are_checked(
    target: dict[int, Mock], fields: dict[str, int], message: str
) -> None:
    """A random list node is not sufficient proof of a blocked k_thread."""
    target[THREAD].__getitem__.side_effect = {"base": _value(fields)}.__getitem__
    with pytest.raises(ValueError, match=message):
        sync.show_object("sem", hex(OBJECT), CONFIG, IMAGE)


@pytest.mark.parametrize(
    "node,head,message",
    [
        ({"next": HEAD, "prev": 0}, {"next": NODE, "prev": NODE}, "previous link"),
        ({"next": HEAD, "prev": HEAD}, {"next": NODE, "prev": HEAD}, "tail link"),
        ({"next": NODE, "prev": HEAD}, {"next": NODE, "prev": NODE}, "Cycle"),
        ({"next": 0, "prev": HEAD}, {"next": NODE, "prev": NODE}, "address"),
        ({"next": HEAD, "prev": HEAD}, {"next": NODE + 1, "prev": NODE}, "address"),
    ],
)
def test_queue_corruption_is_not_silently_truncated(
    target: dict[int, Mock], node: dict[str, int], head: dict[str, int], message: str
) -> None:
    """Cycles and damaged sentinel or node links fail explicitly."""
    target[NODE].__getitem__.side_effect = node.__getitem__
    target[OBJECT]["wait_q"]["waitq"].__getitem__.side_effect = head.__getitem__
    with pytest.raises(ValueError, match=message):
        sync.show_object("sem", hex(OBJECT), CONFIG, IMAGE)


@pytest.mark.parametrize("count", [4096, 4097])
def test_exact_wait_queue_budget(target: dict[int, Mock], fake_gdb: ModuleType, count: int) -> None:
    """Exercise the actual 4096-node limit without shrinking the production constant."""
    last = THREAD + (count - 1) * 128
    target[OBJECT]["wait_q"]["waitq"].__getitem__.side_effect = {
        "next": NODE,
        "prev": last + 20,
    }.__getitem__
    fallback = fake_gdb.Value.side_effect

    def pointer(address: int) -> Mock:
        if THREAD <= address <= last + 20:
            index, offset = divmod(address - THREAD, 128)
            if offset == 20:
                previous = HEAD if not index else address - 128
                following = HEAD if index == count - 1 else address + 128
                result = _value({"prev": previous, "next": following}, address)
            elif offset == 0:
                result = target[THREAD]
            else:
                raise AssertionError(f"Unexpected read 0x{address:X}")
            value = Mock()
            value.cast.return_value.dereference.return_value = result
            return value
        return fallback(address)

    fake_gdb.Value.side_effect = pointer
    if count == 4097:
        with pytest.raises(ValueError, match="exceeds 4096"):
            sync.show_object("sem", hex(OBJECT), CONFIG, IMAGE)
    else:
        assert "4096 waiter(s)" in _render(sync.show_object("sem", hex(OBJECT), CONFIG, IMAGE))


@pytest.mark.parametrize("config", [{}, {"CONFIG_WAITQ_SCALABLE": "y"}])
def test_unknown_or_tree_queue_layout_is_explicitly_unavailable(
    target: dict[int, Mock], fake_gdb: ModuleType, config: dict[str, str]
) -> None:
    """Object fields can still be inspected without claiming complete waiter coverage."""
    output = _render(sync.show_object("sem", hex(OBJECT), config, IMAGE))
    assert "Waiter enumeration unavailable" in output
    assert "0 waiter(s)" not in output
    assert [call.args[0] for call in fake_gdb.Value.call_args_list] == [OBJECT]


@pytest.mark.parametrize("config", [{}, {"CONFIG_EVENTS": "n"}])
def test_disabled_events_fail_before_reading_any_memory(
    target: dict[int, Mock], fake_gdb: ModuleType, config: dict[str, str]
) -> None:
    """A stale events field is not interpreted when the build does not enable events."""
    with pytest.raises(ValueError, match="CONFIG_EVENTS=y"):
        sync.show_object("event", hex(OBJECT), config, IMAGE)
    fake_gdb.Value.assert_not_called()


def test_read_errors_and_unloaded_image_are_not_partial_success(
    target: dict[int, Mock], fake_gdb: ModuleType
) -> None:
    """Image ownership is checked and target errors keep their context."""
    with pytest.raises(ValueError, match="no longer loaded"):
        sync.show_object("sem", hex(OBJECT), CONFIG, Path("/other.elf"))
    fake_gdb.Value.assert_not_called()
    del target[NODE]
    with pytest.raises(ValueError, match="Cannot read Zephyr synchronization.*unreadable"):
        sync.show_object("sem", hex(OBJECT), CONFIG, IMAGE)


@pytest.mark.parametrize("offset", [-4, 124, 128])
def test_invalid_dwarf_node_offset_is_rejected(target: dict[int, Mock], offset: int) -> None:
    """A malformed member address may not escape the complete k_thread structure."""
    target[0x1000]["base"]["qnode_dlist"].address = 0x1000 + offset
    with pytest.raises(ValueError, match="invalid thread wait-node offset"):
        sync.show_object("sem", hex(OBJECT), CONFIG, IMAGE)


def test_nonaddressable_dwarf_value_fails_explicitly(target: dict[int, Mock]) -> None:
    """Optimized-out/nonaddressable fields cannot silently become a null queue."""
    target[OBJECT]["wait_q"].address = None
    with pytest.raises(ValueError, match="no memory address"):
        sync.show_object("sem", hex(OBJECT), CONFIG, IMAGE)


def test_type_in_another_cu_of_same_image_is_accepted(
    target: dict[int, Mock], fake_gdb: ModuleType
) -> None:
    """A kernel compilation unit need not retain types used only by the application."""
    lookup = fake_gdb.lookup_type.side_effect

    def find(name: str, *block: object) -> Mock:
        if block:
            raise fake_gdb.error("No struct type in this compilation unit")
        return lookup(name, None)

    fake_gdb.lookup_type.side_effect = find
    assert "1 waiter(s)" in _render(sync.show_object("sem", hex(OBJECT), CONFIG, IMAGE))


@pytest.mark.parametrize("owner", [None, Mock(filename="/other.elf")])
def test_foreign_or_ownerless_dwarf_types_are_rejected(
    target: dict[int, Mock], fake_gdb: ModuleType, owner: Mock | None
) -> None:
    """Global fallback may never cast memory using a previous project's layout."""
    kind = fake_gdb.lookup_type.side_effect("struct k_sem", None)
    kind.objfile = owner
    with pytest.raises(ValueError, match="not owned by the loaded image"):
        sync.show_object("sem", hex(OBJECT), CONFIG, IMAGE)
    fake_gdb.Value.assert_not_called()


def test_running_target_is_rejected_before_sampling(
    target: dict[int, Mock], fake_gdb: ModuleType
) -> None:
    """All GDB threads must be stopped before reading objects."""
    fake_gdb.selected_inferior.return_value.threads.return_value[0].is_stopped.return_value = False
    with pytest.raises(ValueError, match="stop all"):
        sync.show_object("sem", hex(OBJECT), CONFIG, IMAGE)
    fake_gdb.Value.assert_not_called()


@pytest.mark.parametrize("state", [0, 4, 8, 1, 16, 128])
def test_nonpending_threads_do_not_interpret_stale_queue_or_events(state: int) -> None:
    """Sleep/suspend is not enough to claim an object wait; dead threads have no wait evidence."""
    value = Mock()
    value.__getitem__ = Mock(side_effect=AssertionError("no stale data reads"))
    output = _render(sync.show_thread_wait(value, state))
    assert "unavailable" in output
    value.__getitem__.assert_not_called()


@pytest.mark.parametrize("queue", [0, QUEUE])
def test_pending_thread_reports_queue_without_guessing_object_type(queue: int) -> None:
    """A queue address alone is not a mutex, semaphore, event or a remaining duration."""
    output = _render(sync.show_thread_wait(_value({"base": _value({"pended_on": queue})}), 6))
    if queue:
        assert f"0x{queue:08X}" in output and "type/address unavailable" in output
        assert "not remaining durations" in output
    else:
        assert "Pending with no wait queue" in output


def test_pending_wait_errors_remain_explicit(fake_gdb: ModuleType) -> None:
    """Required pending data failures cannot masquerade as no wait."""
    value = _value({})
    value.__getitem__.side_effect = fake_gdb.MemoryError("denied")
    with pytest.raises(ValueError, match="wait evidence.*denied"):
        sync.show_thread_wait(value, 2)
    with pytest.raises(ValueError, match="address"):
        sync.show_thread_wait(_value({"base": _value({"pended_on": QUEUE + 1})}), 2)


def test_public_command_dispatch_and_error_translation(
    target: dict[int, Mock], fake_gdb: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    """The registered generic command delegates to Zephyr and preserves its failures."""
    command = RtosCmd()
    child = RtosShowObjectCmd(command)
    for args in ([], ["sem"], ["sem", hex(OBJECT), "extra"]):
        with pytest.raises(fake_gdb.GdbError, match="Usage: rtos show object"):
            command._invoke_show_object(args)
    with pytest.raises(fake_gdb.GdbError, match="Select an RTOS and load"):
        child.invoke(f"sem {OBJECT:#x}", False)
    command.state.selected_rtos = zephyr.ZephyrRtos()
    command.state.project = zephyr.ZephyrProject(
        ZephyrBuild("app", "/app", "/zephyr", "4.5.99", "board", ""), CONFIG, (), IMAGE
    )
    child.invoke(f"sem {OBJECT:#x}", False)
    assert "Zephyr sem" in capsys.readouterr().out
    with pytest.raises(fake_gdb.GdbError, match="Cannot inspect RTOS object.*type must"):
        child.invoke(f"pipe {OBJECT:#x}", False)


def test_camelot_and_unsupported_zephyr_versions_remain_gated() -> None:
    """Do not enable new operations for Camelot or unknown kernel ABIs."""
    project = zephyr.ZephyrProject(
        ZephyrBuild("app", "/app", "/zephyr", "4.0.0", "board", ""), CONFIG, (), IMAGE
    )
    assert not project.capabilities.object_inspection
    with pytest.raises(ValueError, match="4.1 through 4.x"):
        project.show_object("sem", hex(OBJECT))
    with pytest.raises(ValueError, match="Camelot.*unavailable"):
        camelot.CamelotProject(Path("/build"), Mock()).show_object("sem", hex(OBJECT))
