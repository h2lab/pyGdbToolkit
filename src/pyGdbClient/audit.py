# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Audit context: what an analysis is about, gathered by the client and pyGdbServer.

The audit context complements the policy-bound ``target.context`` snapshot
with what an auditor, or an LLM backend assisting one, needs to reason about
the analysis: the loaded source project and its declared software identity,
the transferred build artifacts and their digests, the RTOS session, the
execution state, the CPU cores, the SVD memory map and the server session.

Every part is obtained over the existing JSON-RPC methods. A part that cannot
be obtained is recorded with the reason it is unavailable rather than being
omitted or replaced by a default, so consumers never mistake missing data for
an empty target.
"""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import math
from typing import Literal, Self

from pyGdbToolkit.shared.rtos import ProjectDescription
from websockets.exceptions import ConnectionClosed

from .llm.contracts import JsonObject, JsonValue
from .project import LoadedProject, ProjectArtifact, RpcRequester
from .rpc import RpcError

__all__ = [
    "AUDIT_SCHEMA_VERSION",
    "AuditContext",
    "AuditContextError",
    "AuditContextManager",
    "AuditObservation",
    "ProjectContext",
]

AUDIT_SCHEMA_VERSION = 1
MAX_SVD_PERIPHERALS = 1024
_MAX_REASON_CHARS = 240
_NO_PROJECT = "no project is loaded; use the project command"


class AuditContextError(ValueError):
    """pyGdbServer returned data that does not match the expected shape."""


def _json(value: object, where: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_json(item, where) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json(item, where) for key, item in value.items()}
    raise AuditContextError(f"{where} returned values that are not finite JSON")


def _json_object(value: object, where: str) -> JsonObject:
    result = _json(value, where)
    if not isinstance(result, dict):
        raise AuditContextError(f"{where} did not return an object")
    return result


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class AuditObservation:
    """Data obtained from one source, or the explicit reason it is unavailable."""

    source: str
    data: JsonObject | None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        """Require provenance and exactly one of data or an unavailability reason."""
        if not self.source.strip():
            raise ValueError("audit observations require a source")
        if (self.data is None) == (self.unavailable_reason is None):
            raise ValueError("audit observations require either data or an unavailable reason")
        if self.unavailable_reason is not None and not self.unavailable_reason.strip():
            raise ValueError("unavailable audit observations require a reason")

    @classmethod
    def unavailable(cls, source: str, reason: str) -> Self:
        """Return an observation explaining why its source could not be read."""
        return cls(source, None, reason[:_MAX_REASON_CHARS])

    def to_json(self) -> JsonObject:
        """Return the observation in its JSON representation."""
        if self.data is None:
            return {"source": self.source, "unavailable_reason": self.unavailable_reason}
        return {"source": self.source, "data": self.data}


@dataclass(frozen=True)
class ProjectContext:
    """A loaded source project: where it is, what it declares and what was analyzed.

    ``local_root`` is on the auditor's workstation and ``server_path`` on the
    pyGdbServer host. Component ``sources`` and artifact paths are relative to
    ``local_root``; sources absent from the local tree are recorded as None.
    """

    rtos: str
    local_root: str
    server_path: str
    description: ProjectDescription
    artifacts: tuple[ProjectArtifact, ...]
    loaded_at: str

    @classmethod
    def from_loaded(cls, project: LoadedProject, loaded_at: str) -> Self:
        """Describe a committed project transfer, checking which sources exist locally.

        Parameters
        ----------
        project : LoadedProject
            Result of :func:`pyGdbClient.project.load_project`.
        loaded_at : str
            ISO 8601 time at which the project was loaded.

        Returns
        -------
        ProjectContext
            The project context, with unavailable source trees set to None.
        """
        root = project.local_root
        components = tuple(
            (
                component
                if component.sources is None or (root / component.sources).is_dir()
                else replace(component, sources=None)
            )
            for component in project.description.components
        )
        return cls(
            project.rtos,
            str(root),
            project.server_path,
            replace(project.description, components=components),
            project.artifacts,
            loaded_at,
        )

    def to_json(self) -> JsonObject:
        """Return the project context in its JSON representation."""
        return {
            "rtos": self.rtos,
            "local_root": self.local_root,
            "server_path": self.server_path,
            "loaded_at": self.loaded_at,
            **_json_object(self.description.to_dict(), "project description"),
            "artifacts": [
                {"path": artifact.path, "size": artifact.size, "sha256": artifact.sha256}
                for artifact in self.artifacts
            ],
        }


@dataclass(frozen=True)
class AuditContext:
    """Versioned audit snapshot, complementary to the policy-bound target snapshot."""

    captured_at: str
    project: ProjectContext | None
    rtos: AuditObservation
    execution: AuditObservation
    cores: AuditObservation
    svd: AuditObservation
    server: AuditObservation
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        """Require a capture time and a supported schema version."""
        if not self.captured_at.strip() or self.schema_version != AUDIT_SCHEMA_VERSION:
            raise ValueError("audit contexts require a capture time and schema version 1")

    @property
    def unavailable(self) -> tuple[str, ...]:
        """Return the names of the parts that could not be obtained."""
        parts = {
            "rtos": self.rtos,
            "execution": self.execution,
            "cores": self.cores,
            "svd": self.svd,
            "server": self.server,
        }
        missing = ("project",) if self.project is None else ()
        return missing + tuple(name for name, part in parts.items() if part.data is None)

    def to_json(self) -> JsonObject:
        """Return the audit context in its JSON representation."""
        return {
            "schema_version": self.schema_version,
            "captured_at": self.captured_at,
            "project": (
                {"unavailable_reason": _NO_PROJECT}
                if self.project is None
                else self.project.to_json()
            ),
            "rtos": self.rtos.to_json(),
            "execution": self.execution.to_json(),
            "cores": self.cores.to_json(),
            "svd": self.svd.to_json(),
            "server": self.server.to_json(),
        }


def _rtos(data: JsonObject) -> JsonObject:
    if data.get("schema_version") != 1:
        raise AuditContextError("rtos.status returned an unsupported schema version")
    return data


def _svd(data: JsonObject) -> JsonObject | str:
    if data.get("loaded") is not True:
        return "no SVD device is loaded in the GDB session; use svd load"
    peripherals = data.get("peripherals")
    if not isinstance(peripherals, list):
        raise AuditContextError("svd.peripherals returned no peripheral list")
    regions: list[JsonValue] = []
    for peripheral in peripherals[:MAX_SVD_PERIPHERALS]:
        if not isinstance(peripheral, dict):
            raise AuditContextError("svd.peripherals returned a malformed peripheral")
        registers = peripheral.get("registers")
        regions.append(
            {
                "name": peripheral.get("name"),
                "base_address": peripheral.get("base_address"),
                "registers": len(registers) if isinstance(registers, list) else 0,
            }
        )
    return {
        "device": data.get("device"),
        "peripheral_count": len(peripherals),
        "truncated": len(peripherals) > MAX_SVD_PERIPHERALS,
        "peripherals": regions,
    }


def _identity(data: JsonObject) -> JsonObject:
    return data


class AuditContextManager:
    """Hold the audit context of one client session and refresh it from pyGdbServer.

    The manager keeps the loaded project, which only the client knows, and
    gathers every other part from pyGdbServer on :meth:`refresh`. Reading is
    non-intrusive: only status methods are called, never execution control.
    """

    def __init__(self, *, timeout: float = 30.0, clock: Callable[[], str] = _now) -> None:
        """Configure the per-request server timeout and the capture clock."""
        if isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("timeout must be between 0 and 300 seconds")
        self._timeout = float(timeout)
        self._clock = clock
        self._project: ProjectContext | None = None
        self._latest: AuditContext | None = None

    @property
    def project(self) -> ProjectContext | None:
        """Return the loaded project context, if any."""
        return self._project

    @property
    def latest(self) -> AuditContext | None:
        """Return the most recently gathered audit context, if any."""
        return self._latest

    def set_project(self, project: LoadedProject) -> ProjectContext:
        """Record a newly loaded project, replacing any previous one.

        Parameters
        ----------
        project : LoadedProject
            Result of a successful ``project`` command.

        Returns
        -------
        ProjectContext
            The recorded project context.
        """
        self._project = ProjectContext.from_loaded(project, self._clock())
        return self._project

    def clear_project(self) -> None:
        """Forget the loaded project."""
        self._project = None

    async def refresh(self, client: RpcRequester) -> AuditContext:
        """Gather a new audit context from pyGdbServer.

        Parameters
        ----------
        client : RpcRequester
            Connected JSON-RPC client.

        Returns
        -------
        AuditContext
            The new snapshot. Parts the server cannot provide are recorded as
            unavailable with their reason; this method does not raise for them.
        """
        timeout = self._timeout
        rtos, execution, cores, svd, server = await asyncio.gather(
            self._observe(client, "rtos.status", {"timeout": timeout}, _rtos),
            self._observe(client, "target.status", None, _identity),
            self._observe(client, "target.cores", {"timeout": timeout}, _identity),
            self._observe(client, "svd.peripherals", None, _svd),
            self._observe(client, "server.status", None, _identity),
        )
        self._latest = AuditContext(
            self._clock(), self._project, rtos, execution, cores, svd, server
        )
        return self._latest

    async def _observe(
        self,
        client: RpcRequester,
        method: str,
        params: Mapping[str, JsonValue] | None,
        summarize: Callable[[JsonObject], JsonObject | str],
    ) -> AuditObservation:
        source = f"pyGdbServer {method}"
        try:
            result = await client.request(method, params, timeout=self._timeout + 5)
            data = summarize(_json_object(result, method))
        except (RpcError, OSError, ConnectionClosed, AuditContextError) as error:
            return AuditObservation.unavailable(source, str(error) or type(error).__name__)
        if isinstance(data, str):
            return AuditObservation.unavailable(source, data)
        return AuditObservation(source, data)
