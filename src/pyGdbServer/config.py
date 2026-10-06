# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Configuration loading and validation for pyGdbServer."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex
from typing import Any


@dataclass(frozen=True)
class ServerConfig:
    """Validated process and network configuration."""

    gdb_path: str
    gdb_args: tuple[str, ...]
    ocd_path: str
    ocd_args: tuple[str, ...]
    listen_host: str
    listen_port: int
    gdb_init: tuple[str, ...]
    log_directory: Path
    startup_timeout: float
    jlink_core_devices: tuple[str, ...] = ()
    ocd_identifier: str = ""
    jlink_core_ids: tuple[int, ...] = ()

    @property
    def backend_identifier(self) -> str:
        """Use explicit backend identity, retaining executable inference for legacy files."""
        if self.ocd_identifier:
            return self.ocd_identifier
        executable = Path(self.ocd_path).name.lower()
        return next(
            (name for name in ("jlinkgdbserver", "openocd", "pyocd") if name in executable), ""
        )

    def jlink_cluster(self) -> dict[int, str]:
        """Validate configured core IDs and SEGGER device selectors independently of SoC."""
        if not self.jlink_core_devices:
            return {}
        if self.backend_identifier != "jlinkgdbserver":
            raise ValueError(
                "jlink-core-devices requires JLinkGDBServer (ocd-identifier=jlinkgdbserver)"
            )
        devices: dict[int, str] = {}
        if self.jlink_core_ids and len(self.jlink_core_ids) != len(self.jlink_core_devices):
            raise ValueError("jlink-core-devices IDs and devices must have equal lengths")
        for position, device in enumerate(self.jlink_core_devices):
            match = re.search(r"_(\d+)$", device)
            if self.jlink_core_ids:
                core_id = self.jlink_core_ids[position]
            elif match is not None:
                core_id = int(match[1])
            else:
                raise ValueError(
                    "jlink-core-devices without numeric suffixes requires an explicit ID mapping"
                )
            if (
                core_id < 0
                or core_id in devices
                or not device.strip()
                or device in devices.values()
            ):
                raise ValueError(
                    "jlink-core-devices must contain unique non-negative IDs and devices"
                )
            devices[core_id] = device
        if self.ocd_args.count("-device") != 1:
            raise ValueError("J-Link cluster requires exactly one -device argument")
        position = self.ocd_args.index("-device")
        if (
            position + 1 >= len(self.ocd_args)
            or self.ocd_args[position + 1] not in devices.values()
        ):
            raise ValueError("J-Link initial -device must belong to jlink-core-devices")
        if any(
            "reset" in command.lower() or command.lower().startswith("load")
            for command in self.gdb_init
        ):
            raise ValueError(
                "J-Link cluster initialization must not reset or load the shared target"
            )
        return devices

    def jlink_core_command(
        self, device: str, gdb_port: int, telnet_port: int, swo_port: int
    ) -> list[str]:
        """Use dedicated ports and prohibit reset/register initialization for each core."""
        command = self.ocd_command(gdb_port, telnet_port, swo_port)
        for option, value in (
            ("-device", device),
            ("-telnetport", str(telnet_port)),
            ("-swoport", str(swo_port)),
        ):
            if option in command:
                position = command.index(option)
                if position + 1 >= len(command):
                    raise ValueError(f"Missing value for {option}")
                command[position + 1] = value
            else:
                command.extend((option, value))
        if "-ir" in command:
            raise ValueError("J-Link cluster cannot initialize CPU registers (-ir)")
        for option in ("-noreset", "-noir"):
            if option not in command:
                command.append(option)
        return command

    def gdb_connection_type(self) -> str:
        """Select the remote protocol supported by the configured debug server."""
        if self.backend_identifier == "jlinkgdbserver":
            return "remote"
        return "extended-remote"

    def ocd_command(
        self, gdb_port: int, telnet_port: int, swo_port: int | None = None
    ) -> list[str]:
        """Build the OCD command line for a loopback-only dynamic GDB port."""
        values = {
            "gdb_port": str(gdb_port),
            "telnet_port": str(telnet_port),
            "loopback": "127.0.0.1",
        }
        if any("{swo_port}" in argument for argument in self.ocd_args):
            if self.backend_identifier != "jlinkgdbserver":
                raise ValueError("{swo_port} is supported only for ocd-identifier=jlinkgdbserver")
            if swo_port is None or isinstance(swo_port, bool) or not 0 < swo_port <= 65535:
                raise ValueError("{swo_port} requires an allocated SWO port between 1 and 65535")
            values["swo_port"] = str(swo_port)
        has_dynamic_port = any("{gdb_port}" in argument for argument in self.ocd_args)
        arguments = [argument.format_map(values) for argument in self.ocd_args]
        identifier = self.backend_identifier
        if identifier == "pyocd":
            if "--allow-remote" in arguments:
                raise ValueError("pyOCD --allow-remote is forbidden for the private GDB port")
            if not has_dynamic_port:
                if "--port" in arguments or "-p" in arguments:
                    raise ValueError("pyOCD port must use the {gdb_port} placeholder")
                arguments.extend(("--port", str(gdb_port)))
        elif identifier == "openocd":
            bind_commands = [argument.lower() for argument in arguments if "bindto" in argument]
            if any("127.0.0.1" not in command for command in bind_commands):
                raise ValueError("OpenOCD bindto must be 127.0.0.1")
            if not bind_commands:
                arguments.extend(("-c", "bindto 127.0.0.1"))
            if not has_dynamic_port:
                if any("gdb_port" in argument.lower() for argument in arguments):
                    raise ValueError("OpenOCD gdb_port must use the {gdb_port} placeholder")
                arguments.extend(("-c", f"gdb_port {gdb_port}"))
        elif identifier == "jlinkgdbserver":
            if "-localhostonly" in arguments:
                positions = [
                    index
                    for index, argument in enumerate(arguments)
                    if argument == "-localhostonly"
                ]
                if any(
                    index + 1 >= len(arguments) or arguments[index + 1] != "1"
                    for index in positions
                ):
                    raise ValueError("J-Link -localhostonly must be 1")
            else:
                arguments.extend(("-localhostonly", "1"))
            if not has_dynamic_port:
                if "-port" in arguments or "-p" in arguments:
                    raise ValueError("J-Link port must use the {gdb_port} placeholder")
                arguments.extend(("-port", str(gdb_port)))
        else:
            if not has_dynamic_port or not any(
                "{loopback}" in argument for argument in self.ocd_args
            ):
                raise ValueError(
                    "unknown OCDs must use both {gdb_port} and {loopback} placeholders"
                )
        return [self.ocd_path, *arguments]

    def gdb_command(self) -> list[str]:
        """Build a GDB command line using the newest available MI interpreter."""
        return [self.gdb_path, "--quiet", "--nx", "--interpreter=mi3", *self.gdb_args]


def _string_list(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be an array of strings")
    return tuple(value)


def _listen_address(value: object) -> tuple[str, int]:
    if not isinstance(value, str):
        raise ValueError("listen-address must be a host:port string")
    host, separator, port_text = value.rpartition(":")
    if not separator or not host:
        raise ValueError("listen-address must be a host:port string")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        port = int(port_text)
    except ValueError as error:
        raise ValueError("listen-address contains an invalid port") from error
    if not 0 <= port <= 65535:
        raise ValueError("listen-address port must be between 0 and 65535")
    return host, port


def load_config(path: str | Path) -> ServerConfig:
    """Load a pyGdbServer JSON configuration file."""
    config_path = Path(path).resolve()
    with config_path.open(encoding="utf-8") as config_file:
        data = json.load(config_file)
    if not isinstance(data, dict):
        raise ValueError("configuration root must be a JSON object")

    for key in ("gdb-path", "ocd-path"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"{key} must be a non-empty string")

    identifier = data.get("ocd-identifier", "")
    if not isinstance(identifier, str) or (
        "ocd-identifier" in data and identifier not in ("jlinkgdbserver", "openocd", "pyocd")
    ):
        raise ValueError("ocd-identifier must be jlinkgdbserver, openocd or pyocd")
    core_ids: tuple[int, ...] = ()
    core_devices = data.get("jlink-core-devices", [])
    if isinstance(core_devices, dict):
        if not core_devices or any(
            not isinstance(key, str)
            or not re.fullmatch(r"0|[1-9]\d*", key)
            or not isinstance(value, str)
            for key, value in core_devices.items()
        ):
            raise ValueError(
                "jlink-core-devices must map non-negative decimal IDs to device strings"
            )
        core_ids = tuple(int(key) for key in core_devices)
        core_devices = list(core_devices.values())
    elif not isinstance(core_devices, list) or not all(
        isinstance(device, str) for device in core_devices
    ):
        raise ValueError("jlink-core-devices must be an ID mapping or an array of strings")

    listen_host, listen_port = _listen_address(data.get("listen-address", "127.0.0.1:0"))
    log_value = data.get("log-directory", ".pygdbserver-logs")
    if not isinstance(log_value, str):
        raise ValueError("log-directory must be a string")
    log_directory = Path(log_value)
    if not log_directory.is_absolute():
        log_directory = config_path.parent / log_directory

    timeout = data.get("startup-timeout", 15.0)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("startup-timeout must be a positive number")

    config = ServerConfig(
        gdb_path=shlex.split(data["gdb-path"])[0],
        gdb_args=_string_list(data, "gdb-args"),
        ocd_path=shlex.split(data["ocd-path"])[0],
        ocd_args=_string_list(data, "ocd-args"),
        listen_host=listen_host,
        listen_port=listen_port,
        gdb_init=_string_list(data, "gdb-init"),
        log_directory=log_directory.resolve(),
        startup_timeout=float(timeout),
        jlink_core_devices=tuple(core_devices),
        jlink_core_ids=core_ids,
        ocd_identifier=identifier,
    )
    if "jlink-core-devices" in data and config.backend_identifier != "jlinkgdbserver":
        raise ValueError(
            "jlink-core-devices requires JLinkGDBServer (ocd-identifier=jlinkgdbserver)"
        )
    config.jlink_cluster()
    return config
