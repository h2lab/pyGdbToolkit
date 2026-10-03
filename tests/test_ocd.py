# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Read-only OCD detection and connection lifecycle tests."""

from pyGdbToolkit.ocd import OcdDetector, OcdIdentifier, probe_ocd


def test_openocd_version_identifies_server() -> None:
    calls = []

    def execute(command: str) -> str:
        calls.append(command)
        return "Open On-Chip Debugger 0.12.0+dev\nLicensed under GNU GPL v2\n"

    info = probe_ocd(execute)
    assert info.identifier == OcdIdentifier.OPENOCD
    assert info.version == "0.12.0+dev"
    assert calls == ["monitor echo [version]"]


def test_pyocd_inventory_identifies_server() -> None:
    calls = []

    def execute(command: str) -> str:
        calls.append(command)
        if command == "monitor echo [version]":
            raise RuntimeError("unrecognized command 'version'")
        return "2 APs:\n@0x2000: AHB5-AP (selected)\n@0x4000: AHB5-AP\n"

    info = probe_ocd(execute)
    assert info.identifier == OcdIdentifier.PYOCD
    assert info.version is None
    assert calls == ["monitor echo [version]", "monitor show aps"]


def test_unknown_is_not_assumed_to_be_pyocd() -> None:
    info = probe_ocd(lambda command: "unknown command")
    assert info.identifier == OcdIdentifier.UNKNOWN


def test_no_connection_does_not_issue_monitor_requests() -> None:
    def execute(command: str) -> str:
        raise AssertionError(command)

    assert OcdDetector(execute, lambda: None).get().identifier == OcdIdentifier.UNKNOWN


def test_connection_changes_and_disconnect_invalidate_identity() -> None:
    key = [None]
    calls = []

    def execute(command: str) -> str:
        calls.append(command)
        return "Open On-Chip Debugger 0.12.0"

    detector = OcdDetector(execute, lambda: key[0])
    assert detector.get().identifier == OcdIdentifier.UNKNOWN
    key[0] = (1, 1)
    assert detector.get().identifier == OcdIdentifier.OPENOCD
    detector.get()
    assert len(calls) == 1
    key[0] = (2, 2)
    detector.get()
    assert len(calls) == 2
    detector.get(refresh=True)
    assert len(calls) == 3
    key[0] = None
    assert detector.get().identifier == OcdIdentifier.UNKNOWN


def test_unknown_probe_is_retried() -> None:
    replies = iter(["unsupported", "unsupported", "Open On-Chip Debugger 0.12.0"])
    detector = OcdDetector(lambda command: next(replies), lambda: (1, 1))
    assert detector.get().identifier == OcdIdentifier.UNKNOWN
    assert detector.get().identifier == OcdIdentifier.OPENOCD
