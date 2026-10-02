# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Configuration loading and validation for pyGdbServer."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
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
    device: Path | None = None
    usb_serial: str | None = None

    def ocd_command(self, gdb_port: int) -> list[str]:
        """Build the OCD command line for a loopback-only dynamic GDB port."""
        values = {"gdb_port": str(gdb_port), "loopback": "127.0.0.1"}
        if self.device is not None:
            values["device"] = str(self.device)
        if self.usb_serial is not None:
            values["usb_serial"] = self.usb_serial
        has_dynamic_port = any("{gdb_port}" in argument for argument in self.ocd_args)
        try:
            arguments = [argument.format_map(values) for argument in self.ocd_args]
        except KeyError as error:
            raise ValueError(
                f"ocd-args placeholder {{{error.args[0]}}} is unavailable "
                "(it requires --device on a USB serial device)"
            ) from error
        executable = Path(self.ocd_path).name.lower()
        if "pyocd" in executable:
            if "--allow-remote" in arguments:
                raise ValueError("pyOCD --allow-remote is forbidden for the private GDB port")
            if not has_dynamic_port:
                if "--port" in arguments or "-p" in arguments:
                    raise ValueError("pyOCD port must use the {gdb_port} placeholder")
                arguments.extend(("--port", str(gdb_port)))
        elif "openocd" in executable:
            bind_commands = [argument.lower() for argument in arguments if "bindto" in argument]
            if any("127.0.0.1" not in command for command in bind_commands):
                raise ValueError("OpenOCD bindto must be 127.0.0.1")
            if not bind_commands:
                arguments.extend(("-c", "bindto 127.0.0.1"))
            if not has_dynamic_port:
                if any("gdb_port" in argument.lower() for argument in arguments):
                    raise ValueError("OpenOCD gdb_port must use the {gdb_port} placeholder")
                arguments.extend(("-c", f"gdb_port {gdb_port}"))
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


def _usb_serial(device: Path) -> str | None:
    """Return the iSerial of the USB device owning a tty, if any."""
    node = Path("/sys/class/tty") / device.resolve().name / "device"
    if not node.exists():
        return None
    for parent in (node.resolve(), *node.resolve().parents):
        if (parent / "busnum").is_file():
            serial = parent / "serial"
            return serial.read_text(encoding="utf-8").strip() if serial.is_file() else None
    return None


def load_config(
    path: str | Path,
    *,
    device: str | Path | None = None,
    listen_address: str | None = None,
    log_directory: str | Path | None = None,
) -> ServerConfig:
    """Load a pyGdbServer JSON configuration file, with optional overrides."""
    config_path = Path(path).resolve()
    with config_path.open(encoding="utf-8") as config_file:
        data = json.load(config_file)
    if not isinstance(data, dict):
        raise ValueError("configuration root must be a JSON object")
    if listen_address is not None:
        data["listen-address"] = listen_address
    if log_directory is not None:
        data["log-directory"] = str(Path(log_directory).resolve())

    device_path = Path(device) if device is not None else None
    if device_path is not None and not device_path.is_char_device():
        raise ValueError(f"{device_path} is not a character device")

    for key in ("gdb-path", "ocd-path"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise ValueError(f"{key} must be a non-empty string")

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

    return ServerConfig(
        gdb_path=shlex.split(data["gdb-path"])[0],
        gdb_args=_string_list(data, "gdb-args"),
        ocd_path=shlex.split(data["ocd-path"])[0],
        ocd_args=_string_list(data, "ocd-args"),
        listen_host=listen_host,
        listen_port=listen_port,
        gdb_init=_string_list(data, "gdb-init"),
        log_directory=log_directory.resolve(),
        startup_timeout=float(timeout),
        device=device_path,
        usb_serial=_usb_serial(device_path) if device_path is not None else None,
    )
