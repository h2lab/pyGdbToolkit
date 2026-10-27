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
from .llm.confidentiality import Redactor
from .llm.registry import ProviderRegistry, default_registry


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
        help="SQLite file retaining agent conversations and target snapshots (default: memory)",
    )
    agent.add_argument(
        "--llm-redact",
        action="append",
        default=[],
        metavar="REGEX",
        help="Also mask matches of REGEX before disclosure to the provider (repeatable)",
    )
    agent.add_argument(
        "--llm-test-data",
        action="store_true",
        help=(
            "Disclose test data unchanged, including cryptographic material; production data "
            "requires a local model on trusted equipment, not an external provider"
        ),
    )
    agent.add_argument(
        "--llm-context-limit",
        type=int,
        default=AgentSettings.context_limit_chars,
        metavar="CHARS",
        help="Characters exchanged before continuing in a new session (default: %(default)s)",
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
        if (
            arguments.llm_store is not None
            or arguments.llm_redact
            or arguments.llm_test_data
            or arguments.llm_context_limit != AgentSettings.context_limit_chars
        ):
            parser.error(
                "--llm-store, --llm-redact, --llm-test-data and --llm-context-limit require --llm"
            )
        return None
    if arguments.llm_test_data and arguments.llm_redact:
        parser.error("--llm-test-data cannot be combined with --llm-redact")
    try:
        redactor = Redactor.with_patterns(arguments.llm_redact)
        provider = registry.create(arguments.llm, arguments, environ)
    except ValueError as error:
        parser.error(str(error))
    store = arguments.llm_store.expanduser() if arguments.llm_store is not None else None
    try:
        return AgentSettings(
            provider,
            store_path=store,
            redactor=redactor,
            context_limit_chars=arguments.llm_context_limit,
            test_data=arguments.llm_test_data,
        )
    except ValueError as error:
        parser.error(f"--llm-context-limit: {error}")


def main() -> None:
    """Run the interactive dashboard."""
    registry = default_registry()
    parser = _parser(registry)
    arguments = parser.parse_args()
    agent = agent_settings(parser, arguments, registry, os.environ)
    asyncio.run(PyGdbClientApp(arguments.url, agent).run_async())


if __name__ == "__main__":
    main()
