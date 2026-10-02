# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Configuration tests for the standalone pyGdbServer package."""

from dataclasses import replace
import json
from pathlib import Path

import pytest

from pyGdbServer.config import load_config


def _write_config(path: Path, **overrides: object) -> Path:
    data: dict[str, object] = {
        "gdb-path": "gdb-multiarch",
        "gdb-args": [],
        "ocd-path": "pyocd",
        "ocd-args": ["gdbserver"],
        "listen-address": "localhost:1234",
    }
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_load_config_expands_dynamic_pyocd_port(tmp_path: Path) -> None:
    """A legacy pyOCD configuration gains loopback and dynamic port arguments."""
    config = load_config(_write_config(tmp_path / "target.json"))

    assert config.ocd_command(43123) == [
        "pyocd",
        "gdbserver",
        "--port",
        "43123",
    ]
    assert config.listen_host == "localhost"
    assert config.listen_port == 1234


def test_unknown_ocd_requires_dynamic_port_placeholder(tmp_path: Path) -> None:
    """Unknown OCD command lines cannot silently use a fixed target port."""
    config = load_config(_write_config(tmp_path / "target.json", **{"ocd-path": "vendor-ocd"}))

    with pytest.raises(ValueError, match="gdb_port"):
        config.ocd_command(43123)


def test_pyocd_cannot_expose_private_port_remotely(tmp_path: Path) -> None:
    """The private pyOCD GDB endpoint cannot opt into remote access."""
    config = load_config(
        _write_config(tmp_path / "target.json", **{"ocd-args": ["gdbserver", "--allow-remote"]})
    )

    with pytest.raises(ValueError, match="allow-remote"):
        config.ocd_command(43123)


def test_ocd_placeholder_is_expanded(tmp_path: Path) -> None:
    """Explicit command templates support arbitrary OCD implementations."""
    config = load_config(
        _write_config(
            tmp_path / "target.json",
            **{"ocd-path": "vendor-ocd", "ocd-args": ["--bind", "{loopback}:{gdb_port}"]},
        )
    )

    assert config.ocd_command(43123) == ["vendor-ocd", "--bind", "127.0.0.1:43123"]


def test_pyocd_template_rejects_remote_access(tmp_path: Path) -> None:
    """The dynamic-port template cannot override pyOCD's loopback-only default."""
    config = load_config(
        _write_config(
            tmp_path / "target.json",
            **{"ocd-args": ["gdbserver", "--port", "{gdb_port}", "--allow-remote"]},
        )
    )

    with pytest.raises(ValueError, match="allow-remote"):
        config.ocd_command(43123)


def test_command_line_overrides_and_device_placeholders(tmp_path: Path) -> None:
    """One target template serves several instances, each bound to its own probe."""
    config = load_config(
        _write_config(
            tmp_path / "target.json",
            **{"ocd-args": ["gdbserver", "-u", "{usb_serial}", "--port", "{gdb_port}"]},
        ),
        listen_address="0.0.0.0:4001",
        log_directory=tmp_path / "logs",
    )
    config = replace(config, device=Path("/dev/ttyACM3"), usb_serial="0043004832")

    assert (config.listen_host, config.listen_port) == ("0.0.0.0", 4001)
    assert config.log_directory == (tmp_path / "logs").resolve()
    assert config.ocd_command(43123) == ["pyocd", "gdbserver", "-u", "0043004832", "--port", "43123"]


def test_usb_serial_placeholder_requires_device(tmp_path: Path) -> None:
    """A device-bound template fails clearly when started without --device."""
    config = load_config(
        _write_config(tmp_path / "target.json", **{"ocd-args": ["gdbserver", "-u", "{usb_serial}"]})
    )

    with pytest.raises(ValueError, match="usb_serial"):
        config.ocd_command(43123)


def test_device_must_be_a_character_device(tmp_path: Path) -> None:
    """Regular files are not accepted as probe devices."""
    with pytest.raises(ValueError, match="character device"):
        load_config(_write_config(tmp_path / "target.json"), device=tmp_path / "target.json")


def test_openocd_template_always_binds_loopback(tmp_path: Path) -> None:
    """Explicit OpenOCD port templates still receive a loopback bind command."""
    config = load_config(
        _write_config(
            tmp_path / "target.json",
            **{
                "ocd-path": "openocd",
                "ocd-args": ["-c", "gdb_port {gdb_port}"],
            },
        )
    )

    assert config.ocd_command(43123) == [
        "openocd",
        "-c",
        "gdb_port 43123",
        "-c",
        "bindto 127.0.0.1",
    ]
