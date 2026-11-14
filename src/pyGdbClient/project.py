# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Load a local RTOS source project into the remote GDB session through pyGdbServer.

The client ``project`` command stages the build artifacts declared by the
project's manifest over the ``rtos.project.begin``, ``rtos.project.put`` and
``rtos.project.commit`` JSON-RPC methods, then pyGdbServer selects the RTOS
and loads the project in GDB exactly as the GDB-side ``rtos`` command would.
Supported project types come from the shared RTOS project registry, keeping
the client and ``rtos`` synchronized.
"""

import asyncio
import base64
from collections.abc import Callable, Mapping
import contextlib
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Protocol
from uuid import uuid4

from pyGdbToolkit.shared.rtos import (
    DEFAULT_PROJECT_REGISTRY,
    ProjectRegistry,
    matches_project_pattern,
)

__all__ = ["LoadedProject", "ProjectLoadError", "load_project", "supported_project_types"]

_TRANSFER_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_CHUNK_SIZE = 4 * 1024 * 1024


class ProjectLoadError(ValueError):
    """The project cannot be transferred or loaded; the message is safe to display."""


class RpcRequester(Protocol):
    """The part of the JSON-RPC client needed to transfer a project."""

    async def request(
        self, method: str, params: Mapping[str, Any] | None = None, timeout: float = 60.0
    ) -> Any:
        """Send one request and return its result."""
        ...


@dataclass(frozen=True)
class LoadedProject:
    """A committed project transfer and the resulting GDB RTOS session snapshot."""

    rtos: str
    local_root: Path
    server_path: str
    files: tuple[str, ...]
    snapshot: dict[str, Any]

    @property
    def task_names(self) -> tuple[str, ...]:
        """Return the build-time task names reported by the loaded RTOS snapshot."""
        task_list = self.snapshot.get("task_list")
        if not isinstance(task_list, dict):
            return ()
        slots = task_list.get("slots")
        if not isinstance(slots, list):
            return ()
        return tuple(
            slot["task_name"]
            for slot in slots
            if isinstance(slot, dict) and isinstance(slot.get("task_name"), str)
        )


def supported_project_types(
    registry: ProjectRegistry = DEFAULT_PROJECT_REGISTRY,
) -> tuple[str, ...]:
    """Return the project types supported by both this client and the ``rtos`` command."""
    return registry.names


def _transfer_name(root: Path) -> str:
    stem = _TRANSFER_UNSAFE.sub("-", root.name).strip("-")[:48] or "project"
    return f"{stem}-{uuid4().hex[:12]}".lstrip("-_")


def _object(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProjectLoadError(f"pyGdbServer returned a malformed {where}")
    return value


def _relative_paths(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProjectLoadError(f"pyGdbServer returned a malformed {where}")
    return tuple(value)


def _chunk_size(limits: object) -> int:
    size = _object(limits, "transfer limits").get("chunk_size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise ProjectLoadError("pyGdbServer returned an invalid transfer chunk size")
    return min(size, _MAX_CHUNK_SIZE)


def _project_files(
    root: Path, project_file: str, required: tuple[str, ...], patterns: tuple[str, ...]
) -> tuple[str, ...]:
    """Resolve the manifest against the local tree, reporting every gap at once."""
    missing = [path for path in required if not (root / path).is_file()]
    if missing:
        raise ProjectLoadError(f"project is missing required files: {', '.join(sorted(missing))}")
    files = [path for path in required if path != project_file]
    for pattern in patterns:
        matches = sorted(
            candidate.relative_to(root).as_posix()
            for candidate in root.glob(pattern)
            if candidate.is_file()
            and matches_project_pattern(candidate.relative_to(root).as_posix(), pattern)
        )
        if len(matches) != 1:
            raise ProjectLoadError(
                f"project requires exactly one file matching {pattern}; found {len(matches)}"
            )
        files.extend(match for match in matches if match not in files)
    return tuple(files)


async def _upload_file(
    client: RpcRequester, transfer: str, relative: str, path: Path, chunk_size: int
) -> dict[str, Any]:
    """Send one file in contiguous chunks; return the last ``put`` response."""
    size = path.stat().st_size
    offset = 0
    response: dict[str, Any] = {}
    with path.open("rb") as source:
        while True:
            data = await asyncio.to_thread(source.read, chunk_size)
            if not data and offset:
                break
            response = _object(
                await client.request(
                    "rtos.project.put",
                    {
                        "name": transfer,
                        "path": relative,
                        "offset": offset,
                        "size": size,
                        "content": base64.b64encode(data).decode("ascii"),
                    },
                ),
                "transfer response",
            )
            offset += len(data)
            if not data or offset >= size:
                break
    if offset != size or response.get("complete") is not True:
        raise ProjectLoadError(f"file changed while uploading: {relative}")
    return response


async def load_project(
    client: RpcRequester,
    root: Path,
    rtos: str,
    *,
    registry: ProjectRegistry = DEFAULT_PROJECT_REGISTRY,
    progress: Callable[[str], None] | None = None,
    commit_timeout: float = 120.0,
) -> LoadedProject:
    """Transfer a local project to pyGdbServer and load it into the GDB session.

    Parameters
    ----------
    client : RpcRequester
        Connected JSON-RPC client.
    root : Path
        Local project root containing the project declaration file.
    rtos : str
        Project type; one of :func:`supported_project_types`.
    registry : ProjectRegistry
        Shared RTOS project registry used to validate the type and discover
        the project declaration file before any transfer starts.
    progress : Callable[[str], None], optional
        Receives one displayable line per transferred file.
    commit_timeout : float
        Time allowed to GDB to load symbols and task metadata, in seconds.

    Returns
    -------
    LoadedProject
        The transferred files and the server's loaded RTOS snapshot.

    Raises
    ------
    ProjectLoadError
        If the type is unsupported, the local tree does not satisfy the
        manifest, or the server rejects the transfer. Transfers that fail
        before commit are aborted on the server.
    """
    if rtos not in registry.names:
        raise ProjectLoadError(
            f"unsupported project type: {rtos} (supported: {', '.join(registry.names)})"
        )
    root = root.expanduser().resolve()
    project_file = registry.manifest(rtos).project_file
    if not root.is_dir():
        raise ProjectLoadError(f"project root is not a directory: {root}")
    if not (root / project_file).is_file():
        raise ProjectLoadError(f"project root does not contain {project_file}: {root}")
    transfer = _transfer_name(root)
    begun = _object(
        await client.request("rtos.project.begin", {"name": transfer, "rtos": rtos}),
        "transfer bootstrap",
    )
    chunk_size = _chunk_size(begun.get("limits"))
    committing = False
    try:
        if progress is not None:
            progress(f"Uploading {project_file}")
        response = await _upload_file(
            client, transfer, project_file, root / project_file, chunk_size
        )
        manifest = _object(response.get("manifest"), "project manifest")
        files = _project_files(
            root,
            project_file,
            _relative_paths(manifest.get("required"), "project manifest"),
            _relative_paths(manifest.get("required_patterns"), "project manifest"),
        )
        for relative in files:
            if progress is not None:
                progress(f"Uploading {relative}")
            await _upload_file(client, transfer, relative, root / relative, chunk_size)
        committing = True
        snapshot = _object(
            await client.request(
                "rtos.project.commit",
                {"name": transfer, "timeout": commit_timeout},
                timeout=commit_timeout + 10,
            ),
            "RTOS snapshot",
        )
    except BaseException:
        if not committing:
            # Only staging transfers may be aborted; failed commits stay
            # frozen on the server because GDB may reference their files.
            with contextlib.suppress(Exception):
                await client.request("rtos.project.abort", {"name": transfer})
        raise
    server_path = snapshot.get("project_path")
    if snapshot.get("loaded") is not True or not isinstance(server_path, str):
        raise ProjectLoadError("pyGdbServer did not confirm loading the project")
    return LoadedProject(rtos, root, server_path, (project_file, *files), snapshot)
