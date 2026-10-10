# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Command-line automation for one or more pyGdbServer instances."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
import json
import math
from pathlib import Path
import sys
from typing import Any

from websockets.exceptions import InvalidURI

from .farm import FarmOperationError, Target, TestFarm
from .runner import ScenarioExecutionError, run_scenario
from .scenario import CommandStep, load_scenario


def _target(value: str) -> Target:
    identifier, separator, server = value.partition("=")
    if not separator or not server:
        raise argparse.ArgumentTypeError("expected ID=HOST:PORT or ID=ws://HOST:PORT")
    try:
        return Target(identifier, server)
    except (ValueError, InvalidURI) as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be a positive number") from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be a positive finite number")
    return timeout


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pyTestFarm",
        description="Automate commands on identified pyGdbServer instances in parallel.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--target", action="append", type=_target, help="ID=HOST:PORT (repeatable)")
    source.add_argument("--scenario", type=Path, help="Validated YAML or JSON scenario file")
    parser.add_argument(
        "--select", action="append", help="Target id receiving commands (repeatable; default: all)"
    )
    parser.add_argument(
        "--command",
        action="append",
        default=[],
        help="Command to execute (repeatable, in order; default: query server.status)",
    )
    parser.add_argument(
        "--timeout", type=_timeout, default=30.0, help="Per-target timeout in seconds (default: 30)"
    )
    arguments = parser.parse_args(argv)
    if arguments.scenario is not None:
        if arguments.command or arguments.select is not None:
            parser.error("--scenario cannot be combined with --command or --select")
        try:
            arguments.scenario = load_scenario(arguments.scenario)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        return arguments
    identifiers = [target.id for target in arguments.target]
    if len(set(identifiers)) != len(identifiers):
        parser.error("target ids must be unique")
    if arguments.select is not None:
        if len(set(arguments.select)) != len(arguments.select):
            parser.error("--select contains duplicate ids")
        for identifier in arguments.select:
            if identifier not in identifiers:
                parser.error(f"unknown target id: {identifier}")
    return arguments


def _print_command(step: CommandStep, results: dict[str, Any]) -> None:
    for identifier, result in results.items():
        print(f"[{identifier}] {step.action}: {step.command}", flush=True)
        output = result.get("output") if isinstance(result, dict) else None
        if isinstance(output, list) and all(isinstance(item, str) for item in output):
            for item in output:
                for line in item.splitlines():
                    print(f"[{identifier}] {line}", flush=True)
            if not output:
                print(f"[{identifier}] {json.dumps(result, ensure_ascii=False)}", flush=True)
        else:
            print(f"[{identifier}] {json.dumps(result, ensure_ascii=False)}", flush=True)


async def _run(arguments: argparse.Namespace) -> dict[str, Any]:
    if arguments.scenario is not None:
        return await run_scenario(
            arguments.scenario, timeout=arguments.timeout, on_command=_print_command
        )
    async with TestFarm(arguments.target, timeout=arguments.timeout) as farm:
        report: dict[str, Any] = {
            identifier: {"server": target.server, "role": target.role, "results": []}
            for identifier, target in farm.targets.items()
        }
        if not arguments.command:
            results = await farm.request("server.status", targets=arguments.select)
            for identifier, result in results.items():
                report[identifier]["results"].append({"method": "server.status", "result": result})
        for command in arguments.command:
            results = await farm.execute(command, targets=arguments.select)
            for identifier, result in results.items():
                report[identifier]["results"].append({"command": command, "result": result})
        return report


def main(argv: Sequence[str] | None = None) -> None:
    """Run the farm, emitting JSON results or explicit failures with a nonzero exit."""
    arguments = _arguments(argv)
    try:
        report = asyncio.run(_run(arguments))
    except FarmOperationError as error:
        failure: dict[str, Any] = {
            "operation": error.operation,
            "errors": {
                identifier: {"type": type(cause).__name__, "message": str(cause)}
                for identifier, cause in error.errors.items()
            },
            "results": error.results,
        }
        if isinstance(error, ScenarioExecutionError):
            failure.update({"step": error.step, "action": error.action, "report": error.report})
        print(
            json.dumps(failure, ensure_ascii=False),
            file=sys.stderr,
        )
        raise SystemExit(1) from error
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
