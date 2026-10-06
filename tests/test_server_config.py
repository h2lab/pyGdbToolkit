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


@pytest.mark.parametrize(
    "identifier,connection",
    (("pyocd", "extended-remote"), ("openocd", "extended-remote"), ("jlinkgdbserver", "remote")),
)
def test_explicit_ocd_identifier_supports_wrapped_executables(
    tmp_path: Path, identifier: str, connection: str
) -> None:
    """Backend policy depends on declared identity rather than executable naming."""
    config = load_config(
        _write_config(
            tmp_path / "wrapper.json",
            **{
                "ocd-path": "/usr/local/bin/debug-wrapper",
                "ocd-identifier": identifier,
                "ocd-args": [],
            },
        )
    )
    assert config.backend_identifier == identifier
    assert config.gdb_connection_type() == connection
    assert any("4000" in argument for argument in config.ocd_command(4000, 4001))


@pytest.mark.parametrize("identifier", ("invalid", "", None, 1))
def test_invalid_explicit_ocd_identifier_is_rejected(tmp_path: Path, identifier: object) -> None:
    """An explicit identifier cannot silently fall back to executable inference."""
    with pytest.raises(ValueError, match="ocd-identifier"):
        load_config(_write_config(tmp_path / "bad.json", **{"ocd-identifier": identifier}))


@pytest.mark.parametrize("identifier", ("pyocd", "openocd"))
def test_core_devices_node_requires_jlink_even_when_empty(tmp_path: Path, identifier: str) -> None:
    """J-Link-only nodes are rejected for other declared backends even with a J-Link path."""
    with pytest.raises(ValueError, match="jlink-core-devices"):
        load_config(
            _write_config(
                tmp_path / "bad.json",
                **{
                    "ocd-path": "JLinkGDBServer",
                    "ocd-identifier": identifier,
                    "jlink-core-devices": [],
                },
            )
        )


def test_generic_jlink_devices_use_explicit_core_ids(tmp_path: Path) -> None:
    """Arbitrary device selectors and non-contiguous IDs require no CPU-name catalog."""
    config = load_config(
        _write_config(
            tmp_path / "generic.json",
            **{
                "ocd-path": "debug-wrapper",
                "ocd-identifier": "jlinkgdbserver",
                "ocd-args": ["-device", "VendorSecondaryCPU"],
                "jlink-core-devices": {"7": "VendorPrimaryCPU", "12": "VendorSecondaryCPU"},
            },
        )
    )
    assert config.jlink_cluster() == {7: "VendorPrimaryCPU", 12: "VendorSecondaryCPU"}


def test_jlink_cluster_commands_preserve_initial_core_and_isolate_ports(tmp_path: Path) -> None:
    """Explicit cluster devices need not begin at the initially attached core."""
    config = load_config(
        _write_config(
            tmp_path / "cluster.json",
            **{
                "ocd-path": "JLinkGDBServer",
                "ocd-args": [
                    "-device",
                    "MIMX8ML6_A53_2",
                    "-port",
                    "{gdb_port}",
                    "-swoport",
                    "2332",
                ],
                "jlink-core-devices": ["MIMX8ML6_A53_0", "MIMX8ML6_A53_2"],
            },
        )
    )
    assert config.jlink_cluster() == {0: "MIMX8ML6_A53_0", 2: "MIMX8ML6_A53_2"}
    command = config.jlink_core_command("MIMX8ML6_A53_0", 4000, 4001, 4002)
    assert command[command.index("-device") + 1] == "MIMX8ML6_A53_0"
    assert command[command.index("-swoport") + 1] == "4002"
    assert command[command.index("-telnetport") + 1] == "4001"
    assert "-noreset" in command and "-noir" in command


@pytest.mark.parametrize(
    "devices", (["MIMX8ML6_A53_0", "MIMX8ML6_A53_0"], ["Cortex-A53"], ["MIMX8ML6_A53_1"])
)
def test_jlink_cluster_rejects_ambiguous_devices(tmp_path: Path, devices: list[str]) -> None:
    """Do not guess cluster membership or the physical index from a generic CPU name."""
    with pytest.raises(ValueError, match="jlink-core-devices"):
        load_config(
            _write_config(
                tmp_path / "cluster.json",
                **{
                    "ocd-path": "JLinkGDBServer",
                    "ocd-args": ["-device", "MIMX8ML6_A53_0"],
                    "jlink-core-devices": devices,
                },
            )
        )


def test_unknown_ocd_requires_dynamic_port_placeholder(tmp_path: Path) -> None:
    """Unknown OCD command lines cannot silently use a fixed target port."""
    config = load_config(_write_config(tmp_path / "target.json", **{"ocd-path": "vendor-ocd"}))

    with pytest.raises(ValueError, match="gdb_port"):
        config.ocd_command(43123, 43124)


@pytest.mark.parametrize("command", ("monitor reset", "load firmware.elf"))
def test_jlink_cluster_rejects_shared_target_initialization(tmp_path: Path, command: str) -> None:
    """Cluster attachment must not reset or overwrite the running shared target."""
    with pytest.raises(ValueError, match="must not reset or load"):
        load_config(
            _write_config(
                tmp_path / "cluster.json",
                **{
                    "ocd-path": "JLinkGDBServer",
                    "ocd-args": ["-device", "MIMX8ML6_A53_0"],
                    "jlink-core-devices": ["MIMX8ML6_A53_0"],
                    "gdb-init": [command],
                },
            )
        )


def test_jlink_cluster_requires_jlink_backend(tmp_path: Path) -> None:
    """An explicit J-Link cluster is never silently routed through another backend."""
    with pytest.raises(ValueError, match="requires JLinkGDBServer"):
        load_config(
            _write_config(
                tmp_path / "cluster.json",
                **{
                    "jlink-core-devices": ["MIMX8ML6_A53_0"],
                },
            )
        )


def test_jlink_cluster_rejects_register_initialization(tmp_path: Path) -> None:
    """The cluster cannot silently initialize registers in an existing OS context."""
    config = load_config(
        _write_config(
            tmp_path / "cluster.json",
            **{
                "ocd-path": "JLinkGDBServer",
                "ocd-args": ["-device", "MIMX8ML6_A53_0", "-ir"],
                "jlink-core-devices": ["MIMX8ML6_A53_0"],
            },
        )
    )
    with pytest.raises(ValueError, match="cannot initialize"):
        config.jlink_core_command("MIMX8ML6_A53_0", 4000, 4001, 4002)


@pytest.mark.parametrize("executable", ["JLinkGDBServer", "/opt/SEGGER/JLinkGDBServerCLExe"])
def test_jlink_uses_remote_and_dynamic_loopback_port(tmp_path: Path, executable: str) -> None:
    """J-Link requires remote mode and receives a private dynamic endpoint."""
    config = load_config(
        _write_config(
            tmp_path / "target.json",
            **{"ocd-path": executable, "ocd-args": ["-device", "Cortex-M7", "-if", "JTAG"]},
        )
    )
    assert config.gdb_connection_type() == "remote"
    assert config.ocd_command(43123, 43124) == [
        executable,
        "-device",
        "Cortex-M7",
        "-if",
        "JTAG",
        "-localhostonly",
        "1",
        "-port",
        "43123",
    ]


@pytest.mark.parametrize("executable", ["pyocd", "openocd", "vendor-ocd"])
def test_other_ocds_keep_extended_remote(tmp_path: Path, executable: str) -> None:
    """Adding J-Link does not change existing debug-server connections."""
    config = load_config(_write_config(tmp_path / "target.json", **{"ocd-path": executable}))
    assert config.gdb_connection_type() == "extended-remote"


@pytest.mark.parametrize("arguments", [["-port", "2331"], ["-p", "2331"]])
def test_jlink_rejects_fixed_gdb_port(tmp_path: Path, arguments: list[str]) -> None:
    """J-Link configurations cannot bypass dynamic GDB port allocation."""
    config = load_config(
        _write_config(
            tmp_path / "target.json", **{"ocd-path": "JLinkGDBServer", "ocd-args": arguments}
        )
    )
    with pytest.raises(ValueError, match="gdb_port"):
        config.ocd_command(43123, 43124)


@pytest.mark.parametrize("arguments", [["-localhostonly", "0"], ["-localhostonly"]])
def test_jlink_rejects_remote_access(tmp_path: Path, arguments: list[str]) -> None:
    """J-Link's private GDB port must remain loopback-only."""
    config = load_config(
        _write_config(
            tmp_path / "target.json", **{"ocd-path": "JLinkGDBServer", "ocd-args": arguments}
        )
    )
    with pytest.raises(ValueError, match="localhostonly"):
        config.ocd_command(43123, 43124)


def test_jlink_port_template(tmp_path: Path) -> None:
    """Explicit J-Link port and loopback settings are preserved."""
    config = load_config(
        _write_config(
            tmp_path / "target.json",
            **{
                "ocd-path": "JLinkGDBServer",
                "ocd-args": ["-port", "{gdb_port}", "-localhostonly", "1"],
            },
        )
    )
    assert config.ocd_command(43123, 43124) == [
        "JLinkGDBServer",
        "-port",
        "43123",
        "-localhostonly",
        "1",
    ]


def test_jlink_swo_port_template_expands_in_single_and_cluster_modes(tmp_path: Path) -> None:
    """Single-core and per-core commands share the same allocated SWO placeholder."""
    config = load_config(
        _write_config(
            tmp_path / "swo.json",
            **{
                "ocd-path": "debug-wrapper",
                "ocd-identifier": "jlinkgdbserver",
                "ocd-args": [
                    "-device",
                    "VendorCPU",
                    "-port",
                    "{gdb_port}",
                    "-telnetport",
                    "{telnet_port}",
                    "-swoport",
                    "{swo_port}",
                ],
            },
        )
    )
    command = config.ocd_command(43123, 43124, 43125)
    assert command[command.index("-swoport") + 1] == "43125"
    assert not any("{" in argument for argument in command)
    cluster_command = config.jlink_core_command("VendorSecondaryCPU", 43223, 43224, 43225)
    assert cluster_command[cluster_command.index("-swoport") + 1] == "43225"
    assert cluster_command.count("-swoport") == 1


@pytest.mark.parametrize("port", (None, 0, -1, 65536, True))
def test_jlink_swo_template_requires_valid_allocation(tmp_path: Path, port: int | None) -> None:
    """A missing or invalid SWO port fails explicitly instead of expanding an invalid template."""
    config = load_config(
        _write_config(
            tmp_path / "swo.json",
            **{
                "ocd-path": "JLinkGDBServer",
                "ocd-args": ["-swoport", "{swo_port}"],
            },
        )
    )
    with pytest.raises(ValueError, match="allocated SWO port"):
        config.ocd_command(43123, 43124, port)


@pytest.mark.parametrize("identifier", ("openocd", "pyocd"))
def test_swo_template_is_restricted_to_jlink(tmp_path: Path, identifier: str) -> None:
    """Other backends retain their own port configuration conventions."""
    config = load_config(
        _write_config(
            tmp_path / "swo.json",
            **{
                "ocd-identifier": identifier,
                "ocd-args": ["{swo_port}"],
            },
        )
    )
    with pytest.raises(ValueError, match="supported only"):
        config.ocd_command(43123, 43124, 43125)


def test_fixed_jlink_swo_port_remains_explicit(tmp_path: Path) -> None:
    """Legacy single-core fixed ports are not silently overridden by template support."""
    config = load_config(
        _write_config(
            tmp_path / "swo.json",
            **{
                "ocd-path": "JLinkGDBServer",
                "ocd-args": ["-swoport", "2332"],
            },
        )
    )
    command = config.ocd_command(43123, 43124, 43125)
    assert command[command.index("-swoport") + 1] == "2332"


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


def test_imx8mp_m7_jlink_example() -> None:
    """The i.MX8MP M7 example uses J-Link's remote mode and initialization syntax."""
    path = Path(__file__).resolve().parents[1] / "doc/examples/boards/imx8mp-m7-jlink.json"
    config = load_config(path)
    assert config.gdb_connection_type() == "remote"
    assert config.ocd_command(43123, 43124, 43125) == [
        "JLinkGDBServer",
        "-if",
        "JTAG",
        "-device",
        "MIMX8ML6_M7",
        "-speed",
        "1000",
        "-endian",
        "little",
        "-port",
        "43123",
        "-swoport",
        "43125",
        "-telnetport",
        "43124",
        "-localhostonly",
        "1",
    ]
    assert config.gdb_args == ("-ex", "set architecture arm", "-ex", "set endian little")
    assert config.gdb_init == ("monitor reset", "set mem inaccessible-by-default off")
