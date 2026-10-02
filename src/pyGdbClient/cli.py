# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Command-line launcher for pyGdbClient."""

from __future__ import annotations

import argparse
import asyncio

from .app import PyGdbClientApp


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pyGdbClient",
        description="Connect to pyGdbServer with a Rich interactive dashboard.",
    )
    parser.add_argument(
        "url",
        nargs="?",
        default="ws://127.0.0.1:1234",
        help="JSON-RPC WebSocket URL (default: ws://127.0.0.1:1234)",
    )
    return parser.parse_args()


def main() -> None:
    """Run the interactive dashboard."""
    arguments = _arguments()
    asyncio.run(PyGdbClientApp(arguments.url).run_async())


if __name__ == "__main__":
    main()
