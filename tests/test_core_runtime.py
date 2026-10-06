# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Exercise core selection against deterministic GDB connections."""

from types import SimpleNamespace

import pytest

from pyGdbToolkit.core_runtime import CoreSessionState, GdbCoreController
from pyGdbToolkit.ocd import OcdIdentifier
from pyGdbToolkit.session import ToolkitSession


@pytest.fixture
def runtime(fake_gdb, monkeypatch):
    backend = SimpleNamespace(identifier=OcdIdentifier.PYOCD)
    detector = SimpleNamespace(get=lambda: backend)
    session = ToolkitSession()
    controller = GdbCoreController(session, detector)
    inferiors = []
    calls = []
    fail = []

    def make_inferior(number, endpoint):
        inferior = SimpleNamespace(
            num=number,
            connection=(
                SimpleNamespace(num=number + 10, type="extended-remote", details=endpoint)
                if endpoint
                else None
            ),
            architecture=lambda: SimpleNamespace(name=lambda: "armv8-m.main"),
            progspace=SimpleNamespace(filename=None),
        )
        threads = []
        inferior.threads = lambda: threads
        inferiors.append(inferior)
        return inferior, threads

    original, threads = make_inferior(1, "localhost:3333")
    fake_gdb._inferior = original
    selected_thread = []

    def execute(command, to_string=True):
        calls.append(command)
        if command == "monitor show cores":
            return " Number Name Type\n *0 Cortex-M33 Cortex-M33\n  1 Cortex-M33 Cortex-M33\n"
        if command == "add-inferior -no-connection":
            make_inferior(max(item.num for item in inferiors) + 1, None)
        elif command.startswith("inferior "):
            fake_gdb._inferior = next(
                item for item in inferiors if item.num == int(command.split()[1])
            )
        elif command.startswith("target "):
            if fail:
                raise fake_gdb.GdbError("connection refused")
            fake_gdb._inferior.connection = SimpleNamespace(
                num=20, type="extended-remote", details=command.split()[-1]
            )
        elif command.startswith("remove-inferiors "):
            inferiors[:] = [item for item in inferiors if item.num != int(command.split()[1])]
        elif command == "disconnect":
            fake_gdb._inferior.connection = None
        return ""

    monkeypatch.setattr(fake_gdb, "execute", execute, raising=False)
    monkeypatch.setattr(fake_gdb, "inferiors", lambda: inferiors, raising=False)
    monkeypatch.setattr(
        fake_gdb,
        "selected_thread",
        lambda: selected_thread[0] if selected_thread else None,
        raising=False,
    )
    return SimpleNamespace(
        controller=controller,
        session=session,
        backend=backend,
        calls=calls,
        fail=fail,
        inferiors=inferiors,
        threads=threads,
        selected_thread=selected_thread,
    )


def test_pyocd_list_does_not_connect(runtime):
    cores = runtime.controller.list()
    assert [core.id for core in cores] == [0, 1]
    assert [core.endpoint for core in cores] == ["localhost:3333", "localhost:3334"]
    assert [core.selected for core in cores] == [True, False]
    assert not any(command.startswith("target ") for command in runtime.calls)


def test_pyocd_switch_reuses_inferiors_and_invalidates_memory(runtime):
    previous_memory = runtime.session.memory
    assert runtime.controller.select(1).id == 1
    assert runtime.controller.select(0).id == 0
    assert runtime.controller.select(1).id == 1
    assert runtime.calls.count("target extended-remote localhost:3334") == 1
    assert len(runtime.inferiors) == 2
    assert runtime.session.memory is not previous_memory


def test_pyocd_failed_connection_restores_original(runtime, fake_gdb):
    runtime.fail.append(True)
    with pytest.raises(fake_gdb.GdbError, match="connection refused"):
        runtime.controller.select(1)
    assert runtime.controller.current().id == 0
    assert len(runtime.inferiors) == 1


def test_pyocd_reconnect_discards_stale_attachment(runtime):
    runtime.controller.select(1)
    runtime.controller.select(0)
    runtime.inferiors[0].connection.num = 99
    runtime.inferiors[0].connection.details = "localhost:4444"
    assert runtime.controller.list()[1].endpoint == "localhost:4445"
    assert runtime.session.state(CoreSessionState).attachments == {0: (1, 99)}


def test_openocd_switch_uses_physical_names_not_thread_order(runtime):
    runtime.backend.identifier = OcdIdentifier.OPENOCD
    for core_id, thread_id in ((1, 17), (0, 23)):
        thread = SimpleNamespace(name=f"rp2350.cm{core_id}", global_num=thread_id)
        thread.switch = lambda thread=thread: runtime.selected_thread.__setitem__(
            slice(None), [thread]
        )
        runtime.threads.append(thread)
    runtime.selected_thread.append(runtime.threads[1])
    assert runtime.controller.current().id == 0
    selected = runtime.controller.select(1)
    assert selected.id == 1
    assert selected.thread == 17
    assert len(runtime.inferiors) == 1
    assert runtime.calls == []


def test_openocd_rejects_rtos_threads(runtime, fake_gdb):
    runtime.backend.identifier = OcdIdentifier.OPENOCD
    runtime.threads.append(SimpleNamespace(name="worker", global_num=1))
    with pytest.raises(fake_gdb.GdbError, match="hardware-core"):
        runtime.controller.list()


def test_jlink_never_uses_openocd_core_discovery(runtime, fake_gdb):
    """Do not interpret J-Link threads as OpenOCD physical core names."""
    runtime.backend.identifier = OcdIdentifier.JLINK
    with pytest.raises(fake_gdb.GdbError, match="identity is unavailable"):
        runtime.controller.list()
    assert runtime.calls == []


@pytest.mark.parametrize(
    "name", ["Cortex-M0", "Cortex-M0+", "Cortex-M4", "Cortex-M7", "Cortex-M33", "Cortex-M35P"]
)
def test_jlink_attached_core_is_board_independent_and_selection_is_a_noop(runtime, name):
    """Use the reported CPU type without AP discovery, thread switching or memory reads."""
    runtime.backend.identifier = OcdIdentifier.JLINK
    memory = runtime.session.memory
    runtime.controller.register_jlink_core(name)
    cores = runtime.controller.list()
    assert len(cores) == 1
    core = runtime.controller.current()
    assert core.id == 0
    assert core.name == name
    assert core.selected
    assert core.thread is None
    assert core.scope == "attached-core-only"
    assert core.identity_source == "jlink-server-connection-log"
    assert runtime.controller.select(0) == core
    assert runtime.session.memory is memory
    assert runtime.calls == []


def test_jlink_connection_change_invalidates_attached_identity(runtime, fake_gdb):
    """Never carry a CPU identity to a new connection."""
    runtime.backend.identifier = OcdIdentifier.JLINK
    runtime.controller.register_jlink_core("Cortex-M7")
    runtime.inferiors[0].connection.num += 1
    with pytest.raises(fake_gdb.GdbError, match="identity is unavailable"):
        runtime.controller.current()
    assert runtime.calls == []


@pytest.mark.parametrize("name", ["Cortex-A53", "Cortex-R52", "MIMX8ML6_M7", "", "Cortex-M7\n"])
def test_jlink_registration_rejects_non_cortex_m_identities(runtime, fake_gdb, name):
    """A configured device or a different CPU family is not a Cortex-M identity."""
    runtime.backend.identifier = OcdIdentifier.JLINK
    with pytest.raises(fake_gdb.GdbError, match="Cortex-M identity"):
        runtime.controller.register_jlink_core(name)


def test_jlink_other_local_core_ids_are_rejected(runtime, fake_gdb):
    """Do not turn AP indices or RTOS threads into physical CPU identifiers."""
    runtime.backend.identifier = OcdIdentifier.JLINK
    runtime.controller.register_jlink_core("Cortex-M4")
    with pytest.raises(fake_gdb.GdbError, match="not discovered"):
        runtime.controller.select(1)
    assert runtime.calls == []


@pytest.mark.parametrize("architecture", ["aarch64", "armv8-a", "riscv:rv32"])
def test_jlink_registration_rejects_out_of_scope_gdb_architecture(runtime, fake_gdb, architecture):
    """Do not accept Cortex-M metadata on a clearly incompatible GDB connection."""
    runtime.backend.identifier = OcdIdentifier.JLINK
    runtime.inferiors[0].architecture = lambda: SimpleNamespace(name=lambda: architecture)
    with pytest.raises(fake_gdb.GdbError, match="does not include"):
        runtime.controller.register_jlink_core("Cortex-M7")


def test_jlink_identity_survives_svd_cache_invalidation_but_not_reset(runtime, fake_gdb):
    """The attached identity is connection state, not cached target-memory data."""
    runtime.backend.identifier = OcdIdentifier.JLINK
    runtime.controller.register_jlink_core("Cortex-M4")
    runtime.session.invalidate()
    assert runtime.controller.current().name == "Cortex-M4"
    runtime.session.reset()
    with pytest.raises(fake_gdb.GdbError, match="identity is unavailable"):
        runtime.controller.current()


@pytest.mark.parametrize("core_id", [-1, 2, True, "1"])
def test_invalid_core_never_changes_context(runtime, fake_gdb, core_id):
    with pytest.raises(fake_gdb.GdbError):
        runtime.controller.select(core_id)
    assert not any(command.startswith("inferior ") for command in runtime.calls)
