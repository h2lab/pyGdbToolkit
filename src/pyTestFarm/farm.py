# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Manage independent, identified pyGdbServer connections."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Iterable, Mapping
from dataclasses import dataclass
import math
import re
from types import TracebackType
from typing import Any

from websockets.uri import parse_uri

from pyGdbClient.rpc import JsonRpcClient


@dataclass(frozen=True)
class Target:
    """Associate a unique farm-local identifier and optional role with a server."""

    id: str
    server: str
    role: str | None = None

    def __post_init__(self) -> None:
        """Validate identifiers and normalize WebSocket endpoints."""
        if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", self.id) is None:
            raise ValueError("target id must contain only letters, digits, '_', '-' or '.'")
        url = self.server if "://" in self.server else f"ws://{self.server}"
        parse_uri(url)
        object.__setattr__(self, "server", url)


class FarmOperationError(Exception):
    """Expose target-specific failures together with any successful results."""

    def __init__(
        self, operation: str, errors: Mapping[str, Exception], results: Mapping[str, Any]
    ) -> None:
        """Preserve each target's exception and the operation's partial results."""
        self.operation = operation
        self.errors = dict(errors)
        self.results = dict(results)
        details = "; ".join(
            f"{target_id}: {type(error).__name__}: {error}" for target_id, error in errors.items()
        )
        super().__init__(f"{operation} failed ({details})")


def _validate_timeout(timeout: float) -> None:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")


class TestFarm:
    """Run RPC operations concurrently across independent server connections."""

    def __init__(self, targets: Iterable[Target], timeout: float = 30.0) -> None:
        """Register targets without opening connections."""
        _validate_timeout(timeout)
        registered: dict[str, Target] = {}
        for target in targets:
            if target.id in registered:
                raise ValueError(f"duplicate target id: {target.id}")
            registered[target.id] = target
        if not registered:
            raise ValueError("at least one target is required")
        self._targets = registered
        self.timeout = timeout
        self._clients = {target.id: JsonRpcClient(target.server) for target in registered.values()}

    @property
    def targets(self) -> Mapping[str, Target]:
        """Return the registered targets without exposing the mutable registry."""
        return dict(self._targets)

    def _select(self, targets: Iterable[str] | None) -> list[str]:
        identifiers = list(self._targets) if targets is None else list(targets)
        if not identifiers:
            raise ValueError("at least one target must be selected")
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("target selection contains duplicate ids")
        for identifier in identifiers:
            if identifier not in self._targets:
                raise ValueError(f"unknown target id: {identifier}")
        return identifiers

    async def _collect(self, operation: str, calls: Mapping[str, Awaitable[Any]]) -> dict[str, Any]:
        outcomes = await asyncio.gather(*calls.values(), return_exceptions=True)
        results: dict[str, Any] = {}
        errors: dict[str, Exception] = {}
        for identifier, outcome in zip(calls, outcomes):
            if isinstance(outcome, Exception):
                errors[identifier] = outcome
            elif isinstance(outcome, BaseException):
                raise outcome
            else:
                results[identifier] = outcome
        if errors:
            raise FarmOperationError(operation, errors, results)
        return results

    async def connect_all(self) -> None:
        """Connect in parallel, closing all sockets if any connection fails."""
        try:
            await self._collect(
                "connect_all",
                {
                    identifier: asyncio.wait_for(client.connect(), self.timeout)
                    for identifier, client in self._clients.items()
                },
            )
        except BaseException:
            await self.close()
            raise

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        targets: Iterable[str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send an RPC to all or selected targets and return results keyed by id."""
        identifiers = self._select(targets)
        effective_timeout = self.timeout if timeout is None else timeout
        _validate_timeout(effective_timeout)
        return await self._collect(
            method,
            {
                identifier: self._clients[identifier].request(method, params, effective_timeout)
                for identifier in identifiers
            },
        )

    async def execute(
        self,
        command: str,
        *,
        targets: Iterable[str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Execute a toolkit, GDB or monitor command on the selected targets."""
        effective_timeout = self.timeout if timeout is None else timeout
        _validate_timeout(effective_timeout)
        return await self.request(
            "command.execute",
            {"command": command, "timeout": effective_timeout},
            targets=targets,
            timeout=effective_timeout + 5.0,
        )

    def notifications(self, target_id: str) -> asyncio.Queue[dict[str, Any]]:
        """Return one target's notification queue without mixing server events."""
        self._select([target_id])
        return self._clients[target_id].notifications

    async def close(self) -> None:
        """Disconnect clients without shutting down their servers or targets."""
        await self._collect(
            "close", {identifier: client.close() for identifier, client in self._clients.items()}
        )

    async def __aenter__(self) -> TestFarm:
        """Connect every target before entering an automation session."""
        await self.connect_all()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release every connection on normal completion, error or cancellation."""
        await self.close()
