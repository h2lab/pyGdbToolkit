# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Command-line entry point for pyGdbServer."""

from __future__ import annotations

import argparse
import asyncio
import signal
from pathlib import Path

from .config import load_config
from .server import PyGdbServer


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pyGdbServer",
        description="Supervise an OCD and GDB, then expose pyGdbToolkit over JSON-RPC/WebSocket.",
    )
    parser.add_argument("config", type=Path, help="target JSON configuration")
    parser.add_argument(
        "--device",
        type=Path,
        help="probe tty (/dev/ttyACMx, /dev/ttyUSBx); enables {device} and {usb_serial} in ocd-args",
    )
    parser.add_argument("--listen-address", help="override listen-address (host:port)")
    parser.add_argument("--log-directory", type=Path, help="override log-directory")
    return parser.parse_args()


async def _run(arguments: argparse.Namespace) -> None:
    server = PyGdbServer(
        load_config(
            arguments.config,
            device=arguments.device,
            listen_address=arguments.listen_address,
            log_directory=arguments.log_directory,
        )
    )
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_number, server._shutdown.set)
    try:
        await server.start()
        status = server.status()
        print(
            f"pyGdbServer ready at ws://{status['api']['host']}:{status['api']['port']} "
            f"(logs: {status['log_file']})",
            flush=True,
        )
        await server.wait_closed()
    finally:
        await server.stop()


def main() -> None:
    """Run pyGdbServer from its JSON configuration."""
    asyncio.run(_run(_arguments()))


if __name__ == "__main__":
    main()
