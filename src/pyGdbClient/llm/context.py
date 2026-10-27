# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Fetch and strictly decode the ``target.context`` snapshot served by pyGdbServer."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from .contracts import ContextFact, MemoryMapContext, MemoryRegion, TargetContext

__all__ = ["TargetContextError", "fetch_target_context", "parse_target_context"]


class TargetContextError(ValueError):
    """The server returned a snapshot that does not match the supported schema."""


class RpcRequester(Protocol):
    """The part of the JSON-RPC client needed to request a snapshot."""

    async def request(
        self, method: str, params: Mapping[str, Any] | None = None, timeout: float = 60.0
    ) -> Any:
        """Send one request and return its result."""
        ...


def _object(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TargetContextError(f"{where} must be an object")
    return value


def _optional_string(data: dict[str, Any], key: str, where: str) -> str | None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        raise TargetContextError(f"{where}.{key} must be a string or null")
    return value


def _string(data: dict[str, Any], key: str, where: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise TargetContextError(f"{where}.{key} must be a string")
    return value


def _fact(value: object, where: str) -> ContextFact:
    data = _object(value, where)
    try:
        return ContextFact(
            value=_optional_string(data, "value", where),
            source=_string(data, "source", where),
            unavailable_reason=_optional_string(data, "unavailable_reason", where),
        )
    except ValueError as error:
        raise TargetContextError(f"{where}: {error}") from error


def _address(data: dict[str, Any], key: str, where: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TargetContextError(f"{where}.{key} must be an integer")
    return value


def _region(value: object, where: str) -> MemoryRegion:
    data = _object(value, where)
    try:
        return MemoryRegion(
            start=_address(data, "start", where),
            end=_address(data, "end", where),
            kind=_string(data, "kind", where),
            source=_string(data, "source", where),
            name=_optional_string(data, "name", where) or "",
            protection=_optional_string(data, "protection", where) or "unknown",
        )
    except ValueError as error:
        raise TargetContextError(f"{where}: {error}") from error


def _memory_map(value: object) -> MemoryMapContext:
    data = _object(value, "memory_map")
    regions = data.get("regions")
    if not isinstance(regions, list):
        raise TargetContextError("memory_map.regions must be an array")
    try:
        return MemoryMapContext(
            regions=tuple(
                _region(region, f"memory_map.regions[{index}]")
                for index, region in enumerate(regions)
            ),
            source=_string(data, "source", "memory_map"),
            unavailable_reason=_optional_string(data, "unavailable_reason", "memory_map"),
        )
    except TargetContextError:
        raise
    except ValueError as error:
        raise TargetContextError(f"memory_map: {error}") from error


def parse_target_context(payload: object) -> TargetContext:
    """Decode a ``target.context`` result, rejecting anything outside schema version 1.

    Parameters
    ----------
    payload : object
        Result of the ``target.context`` JSON-RPC method.

    Returns
    -------
    TargetContext
        The validated snapshot. Unknown additional keys are ignored.

    Raises
    ------
    TargetContextError
        If the schema version is unsupported or a field is missing or malformed.
    """
    data = _object(payload, "target context")
    version = data.get("schema_version")
    if isinstance(version, bool) or version != 1:
        raise TargetContextError(f"unsupported target context schema version: {version!r}")
    try:
        return TargetContext(
            context_id=_string(data, "context_id", "target context"),
            mcu=_fact(data.get("mcu"), "mcu"),
            revision=_fact(data.get("revision"), "revision"),
            architecture=_fact(data.get("architecture"), "architecture"),
            debug_transport=_fact(data.get("debug_transport"), "debug_transport"),
            ocd=_fact(data.get("ocd"), "ocd"),
            core=_fact(data.get("core"), "core"),
            memory_map=_memory_map(data.get("memory_map")),
            svd_file=_fact(data.get("svd_file"), "svd_file"),
        )
    except TargetContextError:
        raise
    except ValueError as error:
        raise TargetContextError(str(error)) from error


async def fetch_target_context(client: RpcRequester, timeout: float = 30.0) -> TargetContext:
    """Request and decode the current target snapshot.

    Parameters
    ----------
    client : RpcRequester
        Connected JSON-RPC client.
    timeout : float
        Maximum time allowed to GDB to collect the snapshot, in seconds.

    Returns
    -------
    TargetContext
        The validated snapshot.
    """
    result = await client.request("target.context", {"timeout": timeout}, timeout=timeout + 5)
    return parse_target_context(result)
