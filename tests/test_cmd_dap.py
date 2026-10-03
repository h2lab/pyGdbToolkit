"""Command, report and session tests independent of a physical probe."""

from dataclasses import replace
import json
from unittest.mock import Mock

import pytest

from pyGdbToolkit.arch.dap import AccessPortProfile, AccessPortRegistry
from pyGdbToolkit.cmd_dap import DapCmd, DapSessionState, DapSubcommand
from pyGdbToolkit.debug_port import AccessPort, DebugPortError
from pyGdbToolkit.session import ToolkitSession


class Transport:
    def __init__(self) -> None:
        self.ports = (AccessPort(0, "Example", True), AccessPort(3, "Example"))
        self.selected = []
        self.fail_selection = False

    def list_access_ports(self):
        return self.ports

    def select_access_port(self, index):
        if self.fail_selection:
            raise DebugPortError("selection failed")
        self.selected.append(index)
        self.ports = tuple(replace(port, selected=port.index == index) for port in self.ports)

    def read_ap(self, index, address):
        raise AssertionError("Only architecture providers may read registers")


class Provider:
    def __init__(self) -> None:
        self.profiled = []

    def supports(self, architecture):
        return architecture == "example"

    def profile(self, transport, port):
        self.profiled.append(port.index)
        return AccessPortProfile(
            port.index,
            "example",
            "Example-AP",
            {},
            {"memory_access": None},
            {},
            {"IDR": "unavailable"},
        )


@pytest.fixture
def command(monkeypatch):
    transport = Transport()
    provider = Provider()
    command = DapCmd(transport, AccessPortRegistry((provider,)), ToolkitSession())
    monkeypatch.setattr(command, "_architecture", lambda: "example")
    return command, transport, provider


def test_command_is_architecture_neutral(command):
    parent, transport, provider = command
    parent.run("profile", ["3"])
    assert provider.profiled == [3]
    assert transport.selected == []


def test_default_profile_follows_server_selection(command):
    parent, transport, provider = command
    parent.run("select", ["0x3"])
    parent.run("profile", [])
    assert transport.selected == [3]
    assert provider.profiled == [3]
    transport.select_access_port(0)
    parent.run("profile", [])
    assert provider.profiled == [3, 0]


def test_report_is_fresh_and_preserves_selection(command, tmp_path):
    parent, transport, provider = command
    path = tmp_path / "ap report.json"
    DapSubcommand(parent, "report").invoke(f'"{path}"', False)
    report = json.loads(path.read_text())
    assert report["schema_version"] == 1
    assert report["selected_ap"] == 0
    assert len(report["access_ports"]) == 2
    assert report["access_ports"][0]["errors"] == {"IDR": "unavailable"}
    assert report["access_ports"][0]["capabilities"]["memory_access"] is None
    assert provider.profiled == [0, 3]
    assert not transport.selected
    parent.collect_report()
    assert provider.profiled == [0, 3, 0, 3]


@pytest.mark.parametrize(
    "name,args",
    [
        ("list", ["0"]),
        ("help", ["0"]),
        ("select", []),
        ("select", ["0", "3"]),
        ("profile", ["0", "3"]),
        ("report", []),
        ("core", ["0", "1"]),
    ],
)
def test_usage_errors(command, fake_gdb, name, args):
    parent, _, _ = command
    with pytest.raises(fake_gdb.GdbError, match="Usage"):
        parent.run(name, args)


@pytest.mark.parametrize("token", ["-1", "1", "not-an-index", "0xZZ"])
def test_invalid_indices_do_not_select(command, fake_gdb, token):
    parent, transport, _ = command
    with pytest.raises(fake_gdb.GdbError):
        parent.run("select", [token])
    assert not transport.selected


def test_failed_selection_does_not_update_state(command, fake_gdb):
    parent, transport, _ = command
    transport.fail_selection = True
    with pytest.raises(fake_gdb.GdbError, match="selection failed"):
        parent.run("select", ["3"])
    assert parent.session.state(DapSessionState).selected_index == 0


def test_session_reset_clears_selection(command):
    parent, _, _ = command
    parent.run("select", ["3"])
    parent.session.reset()
    assert parent.session.state(DapSessionState).selected_index is None


def test_unsupported_architecture(command, monkeypatch, fake_gdb):
    parent, _, _ = command
    monkeypatch.setattr(parent, "_architecture", lambda: "riscv")
    with pytest.raises(fake_gdb.GdbError, match="unsupported"):
        parent.run("profile", [])


def test_report_write_error(command, tmp_path, fake_gdb):
    parent, _, _ = command
    with pytest.raises(fake_gdb.GdbError):
        parent.run("report", [str(tmp_path / "missing" / "report.json")])


def test_all_subcommands_registered(fake_gdb):
    parent = DapCmd()
    for name in ("list", "core", "select", "profile", "report", "help"):
        DapSubcommand(parent, name)
    registrations = fake_gdb.Command.registrations
    assert {name for name, _ in registrations} == {
        "dap",
        "dap list",
        "dap core",
        "dap select",
        "dap profile",
        "dap report",
        "dap help",
    }
    helps = {help_entry.name: help_entry for help_entry in parent.session.commands}
    assert "dap" in helps
    assert "ap" not in helps
    assert all(entry.syntax.startswith("dap ") for entry in helps["dap"].usage)


def test_core_command_dispatch_and_ap_invalidation(command):
    parent, _, _ = command
    parent.cores = Mock()
    parent.cores.list.return_value = []
    parent.run("select", ["3"])
    parent.run("core", ["list"])
    parent.cores.list.assert_called_once_with()
    parent.run("core", [])
    parent.cores.current.assert_called_once_with()
    parent.run("core", ["1"])
    parent.cores.select.assert_called_once_with(1)
    assert parent.session.state(DapSessionState).selected_index is None


@pytest.mark.parametrize("token", ["-1", "1.2", "unknown"])
def test_core_command_rejects_invalid_tokens(command, fake_gdb, token):
    parent, _, _ = command
    parent.cores = Mock()
    with pytest.raises(fake_gdb.GdbError, match="Core ID"):
        parent.run("core", [token])
    parent.cores.select.assert_not_called()
