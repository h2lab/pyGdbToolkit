#!/usr/bin/python3
# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Start configured pyGdbServer instances whose device nodes are present."""

from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import subprocess
import sys


def _device_path(config_path: Path) -> Path:
    """Read the literal DEVICE_PATH assignment without sourcing the file."""
    value: str | None = None
    for line in config_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, raw_value = line.partition("=")
        if separator and key.strip() == "DEVICE_PATH":
            value = raw_value.strip()
    if value is None:
        raise ValueError("missing DEVICE_PATH assignment")
    fields = shlex.split(value, comments=True)
    if len(fields) != 1 or not fields[0].startswith("/dev/"):
        raise ValueError("DEVICE_PATH must be one absolute /dev path")
    return Path(fields[0])


def _server_config_path(config_path: Path) -> Path:
    """Read the JSON configuration path assigned to this instance."""
    value: str | None = None
    for line in config_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, raw_value = line.partition("=")
        if separator and key.strip() == "PYGDBSERVER_CONFIG":
            value = raw_value.strip()
    if value is None:
        raise ValueError("missing PYGDBSERVER_CONFIG assignment")
    fields = shlex.split(value, comments=True)
    if len(fields) != 1 or not Path(fields[0]).is_absolute():
        raise ValueError("PYGDBSERVER_CONFIG must be one absolute path")
    server_config = Path(fields[0])
    expected_config = config_path.with_suffix(".json")
    if server_config != expected_config:
        raise ValueError(f"PYGDBSERVER_CONFIG must point to paired file {expected_config}")
    return server_config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, default=Path("/etc/pygdbserver"))
    parser.add_argument("--systemctl", default="/usr/bin/systemctl")
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()

    configs = sorted(arguments.config_dir.glob("*.conf"))
    if not configs:
        print(f"No instance configurations in {arguments.config_dir}; nothing to start.")
        return 0

    failures = 0
    for config_path in configs:
        if not config_path.is_file():
            continue
        try:
            device = _device_path(config_path)
        except (OSError, ValueError) as error:
            print(f"Skipping {config_path}: {error}", file=sys.stderr)
            failures += 1
            continue
        if not device.is_char_device():
            print(f"Skipping {config_path}: device {device} is absent")
            continue

        try:
            server_config = _server_config_path(config_path)
        except (OSError, ValueError) as error:
            print(f"Skipping {config_path}: {error}", file=sys.stderr)
            failures += 1
            continue
        if not server_config.is_file():
            print(f"Skipping {config_path}: JSON config {server_config} is absent", file=sys.stderr)
            failures += 1
            continue

        unit = f"pygdbserver@{config_path.stem}.service"
        command = [arguments.systemctl, "start", "--no-block", unit]
        if arguments.dry_run:
            print(" ".join(shlex.quote(part) for part in command))
            continue
        result = subprocess.run(command, check=False)
        if result.returncode:
            print(f"Failed to queue {unit}", file=sys.stderr)
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
