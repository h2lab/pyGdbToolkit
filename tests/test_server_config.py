# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Configuration tests for the standalone pyGdbServer package."""

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

    assert config.ocd_command(43123, 43124) == [
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
        config.ocd_command(43123, 43124)


def test_pyocd_cannot_expose_private_port_remotely(tmp_path: Path) -> None:
    """The private pyOCD GDB endpoint cannot opt into remote access."""
    config = load_config(
        _write_config(tmp_path / "target.json", **{"ocd-args": ["gdbserver", "--allow-remote"]})
    )

    with pytest.raises(ValueError, match="allow-remote"):
        config.ocd_command(43123, 43124)


def test_ocd_placeholder_is_expanded(tmp_path: Path) -> None:
    """Explicit command templates support arbitrary OCD implementations."""
    config = load_config(
        _write_config(
            tmp_path / "target.json",
            **{"ocd-path": "vendor-ocd", "ocd-args": ["--bind", "{loopback}:{gdb_port}"]},
        )
    )

    assert config.ocd_command(43123, 43124) == ["vendor-ocd", "--bind", "127.0.0.1:43123"]


def test_pyocd_template_rejects_remote_access(tmp_path: Path) -> None:
    """The dynamic-port template cannot override pyOCD's loopback-only default."""
    config = load_config(
        _write_config(
            tmp_path / "target.json",
            **{"ocd-args": ["gdbserver", "--port", "{gdb_port}", "--allow-remote"]},
        )
    )

    with pytest.raises(ValueError, match="allow-remote"):
        config.ocd_command(43123, 43124)


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

    assert config.ocd_command(43123, 43124) == [
        "openocd",
        "-c",
        "gdb_port 43123",
        "-c",
        "bindto 127.0.0.1",
    ]


@pytest.mark.parametrize(
    ("executable", "arguments", "expected"),
    [
        (
            "pyocd",
            ["gdbserver", "--port", "{gdb_port}", "-T", "{telnet_port}"],
            ["pyocd", "gdbserver", "--port", "43123", "-T", "43124"],
        ),
        (
            "openocd",
            ["-c", "gdb_port {gdb_port}", "-c", "telnet_port {telnet_port}"],
            [
                "openocd",
                "-c",
                "gdb_port 43123",
                "-c",
                "telnet_port 43124",
                "-c",
                "bindto 127.0.0.1",
            ],
        ),
    ],
)
def test_telnet_port_placeholder(
    tmp_path: Path, executable: str, arguments: list[str], expected: list[str]
) -> None:
    """Both supported OCDs expand independently selected GDB and Telnet ports."""
    config = load_config(
        _write_config(tmp_path / "target.json", **{"ocd-path": executable, "ocd-args": arguments})
    )

    assert config.ocd_command(43123, 43124) == expected


def test_pico2w_openocd_example() -> None:
    """The Pico 2 W example uses CMSIS-DAP and dynamic loopback endpoints."""
    path = Path(__file__).resolve().parents[1] / "doc/examples/boards/pico2w-openocd.json"
    config = load_config(path)
    command = config.ocd_command(43123, 43124)
    assert "openocd" in Path(config.ocd_path).name
    assert "interface/cmsis-dap.cfg" in command
    assert "target/rp2350.cfg" in command
    assert "adapter serial E6647C74034BC430" in command
    assert "bindto 127.0.0.1" in command
    assert "gdb_port 43123" in command
    assert "telnet_port 43124" in command
    assert "tcl_port disabled" in command
