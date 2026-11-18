# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Execute validated scenario actions using the existing JSON-RPC methods."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import re
from typing import Any

from .farm import FarmOperationError, TestFarm
from .scenario import (
    CommandStep,
    ConnectAll,
    PeerFailure,
    ResetSequence,
    RunUntil,
    Scenario,
    Step,
    WaitAndObserve,
)


class ScenarioExecutionError(FarmOperationError):
    """Include the failed step and the completed scenario report in an error."""

    def __init__(
        self, report: dict[str, Any], index: int, action: str, cause: FarmOperationError
    ) -> None:
        """Retain target errors, partial results and previously completed steps."""
        super().__init__(cause.operation, cause.errors, cause.results)
        self.report = report
        self.step = index
        self.action = action


def _protocol_error(identifier: str, message: str, result: Any) -> FarmOperationError:
    return FarmOperationError("scenario", {identifier: RuntimeError(message)}, {identifier: result})


async def _logs(farm: TestFarm, identifier: str, since: int | None = None) -> list[dict[str, Any]]:
    result = (
        await farm.request("logs.get", {"since": since or 0, "limit": 10_000}, targets=[identifier])
    )[identifier]
    if not isinstance(result, list):
        raise _protocol_error(identifier, "logs.get did not return a list", result)
    previous = since
    events: list[dict[str, Any]] = []
    for event in result:
        if (
            not isinstance(event, dict)
            or isinstance(event.get("sequence"), bool)
            or not isinstance(event.get("sequence"), int)
            or event["sequence"] < 1
            or not all(isinstance(event.get(key), str) for key in ("source", "stream", "message"))
        ):
            raise _protocol_error(identifier, "invalid logs.get event", result)
        if previous is not None and event["sequence"] != previous + 1:
            raise _protocol_error(
                identifier, f"log sequence gap after {previous}: observation is incomplete", result
            )
        previous = event["sequence"]
        events.append(event)
    return events


async def _cursor(farm: TestFarm, identifier: str) -> int:
    events = await _logs(farm, identifier)
    return int(events[-1]["sequence"]) if events else 0


async def _run_until(farm: TestFarm, step: RunUntil) -> dict[str, Any]:
    timeout = step.timeout_ms / 1000 if step.timeout_ms is not None else farm.timeout
    try:
        async with asyncio.timeout(timeout):
            cursor = await _cursor(farm, step.target)
            await farm.execute(
                f"gdb until *{step.address:#x} &",
                targets=[step.target],
                timeout=min(timeout, 300.0),
            )
            while True:
                events = await _logs(farm, step.target, cursor)
                for event in events:
                    cursor = event["sequence"]
                    record = event["message"]
                    if (
                        event["source"] != "gdb"
                        or event["stream"] != "mi"
                        or not record.startswith("*stopped")
                    ):
                        continue
                    reason = re.search(r'\breason="([^"]+)"', record)
                    address = re.search(r'\bframe=\{[^{}]*?\baddr="(0x[0-9a-fA-F]+)"', record)
                    if (
                        reason is not None
                        and reason[1] in {"location-reached", "breakpoint-hit"}
                        and address is not None
                        and int(address[1], 16) == step.address
                    ):
                        return {"target": step.target, "address": step.address, "event": event}
                    raise _protocol_error(
                        step.target, f"target stopped before reaching {step.address:#x}", event
                    )
                await asyncio.sleep(0.05)
    except TimeoutError as error:
        raise FarmOperationError(
            "run_until",
            {step.target: TimeoutError(f"address {step.address:#x} not reached within {timeout}s")},
            {},
        ) from error


async def _observe(farm: TestFarm, step: WaitAndObserve) -> dict[str, Any]:
    cursors = {identifier: await _cursor(farm, identifier) for identifier in step.monitor}
    observed: dict[str, list[dict[str, Any]]] = {identifier: [] for identifier in step.monitor}
    samples: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + step.duration_ms / 1000
    while True:
        statuses = await farm.request("target.status", targets=step.monitor)
        for identifier, status in statuses.items():
            if not isinstance(status, dict) or status.get("state") not in {"running", "stopped"}:
                raise _protocol_error(
                    identifier, "target.status returned an unavailable execution state", status
                )
        samples.append({"elapsed_ms": round((loop.time() - started) * 1000), "targets": statuses})
        for identifier in step.monitor:
            events = await _logs(farm, identifier, cursors[identifier])
            observed[identifier].extend(events)
            if events:
                cursors[identifier] = events[-1]["sequence"]
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(step.poll_interval_ms / 1000, remaining))
    return {"duration_ms": step.duration_ms, "samples": samples, "events": observed}


async def _execute_step(farm: TestFarm, step: Step) -> dict[str, Any]:
    match step:
        case ConnectAll():
            await farm.connect_all()
            return await farm.request("server.status")
        case ResetSequence():
            results = await farm.execute("monitor reset halt", targets=step.targets)
            await asyncio.sleep(step.delay_ms / 1000)
            return {"targets": results, "delay_ms": step.delay_ms}
        case RunUntil():
            return await _run_until(farm, step)
        case PeerFailure():
            return await farm.execute(f"monitor {step.command}", targets=[step.target])
        case WaitAndObserve():
            return await _observe(farm, step)
        case CommandStep():
            return await farm.execute(step.command, targets=step.targets)


async def run_scenario(
    scenario: Scenario,
    timeout: float = 30.0,
    *,
    on_command: Callable[[CommandStep, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run ordered steps, stopping on errors and always releasing the farm sockets."""
    farm = TestFarm(scenario.targets, timeout=timeout)
    report: dict[str, Any] = {
        "scenario": scenario.name,
        "targets": {
            target.id: {"server": target.server, "role": target.role} for target in scenario.targets
        },
        "steps": [],
    }
    try:
        for index, step in enumerate(scenario.steps, start=1):
            try:
                result = await _execute_step(farm, step)
            except FarmOperationError as error:
                if isinstance(step, CommandStep) and on_command is not None:
                    on_command(step, error.results)
                raise ScenarioExecutionError(report, index, step.action, error) from error
            if isinstance(step, CommandStep) and on_command is not None:
                on_command(step, result)
            report["steps"].append({"index": index, "action": step.action, "result": result})
        return report
    finally:
        await farm.close()
