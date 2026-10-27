# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Command-line launcher for pyGdbClient."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
import os
from pathlib import Path

from .app import PyGdbClientApp
from .llm.controller import AgentSettings
from .llm.registry import ProviderConfigurationError, ProviderRegistry, default_registry


def _parser(registry: ProviderRegistry) -> argparse.ArgumentParser:
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
    agent = parser.add_argument_group("Agent CLI")
    agent.add_argument(
        "--llm",
        choices=registry.ids,
        metavar="PROVIDER",
        help=f"Enable the agent CLI with this provider ({', '.join(registry.ids)})",
    )
    agent.add_argument(
        "--llm-store",
        type=Path,
        metavar="FILE",
        help="SQLite file retaining agent conversations and target snapshots (default: none)",
    )
    registry.add_arguments(parser)
    return parser


def agent_settings(
    parser: argparse.ArgumentParser,
    arguments: argparse.Namespace,
    registry: ProviderRegistry,
    environ: Mapping[str, str],
) -> AgentSettings | None:
    """Build the agent configuration, reporting invalid options through the parser."""
    if arguments.llm is None:
        if arguments.llm_store is not None:
            parser.error("--llm-store requires --llm")
        return None
    try:
        provider = registry.create(arguments.llm, arguments, environ)
    except ProviderConfigurationError as error:
        parser.error(str(error))
    store = arguments.llm_store.expanduser() if arguments.llm_store is not None else None
    return AgentSettings(provider, store_path=store)


def main() -> None:
    """Run the interactive dashboard."""
    registry = default_registry()
    parser = _parser(registry)
    arguments = parser.parse_args()
    agent = agent_settings(parser, arguments, registry, os.environ)
    asyncio.run(PyGdbClientApp(arguments.url, agent).run_async())


if __name__ == "__main__":
    main()
