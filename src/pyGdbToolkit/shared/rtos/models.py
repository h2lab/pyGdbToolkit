# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""GDB-independent RTOS snapshots, manifests and project path validation."""

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import TypedDict


class TaskSlotSnapshot(TypedDict):
    """JSON representation of one build-time task metadata slot."""

    index: int
    address: int
    task_name: str | None
    text_address: int
    ram_address: int
    metadata: dict[str, int | list[int] | str]


class TaskListSnapshot(TypedDict):
    """JSON representation of the kernel's build-time task table."""

    address: int
    size: int
    entry_size: int
    slots: list[TaskSlotSnapshot]


class SchedulerSnapshot(TypedDict):
    """Progress of an armed scheduler trace."""

    limit: int
    elections: list[str]


class RtosSnapshot(TypedDict):
    """Versioned, non-intrusive GDB RTOS session snapshot."""

    schema_version: int
    selected_rtos: str | None
    loaded: bool
    project_path: str | None
    task_list: TaskListSnapshot | None
    scheduler_trace: SchedulerSnapshot | None


def project_relative_path(value: object) -> str:
    """Validate a canonical, relative POSIX project file path.

    Parameters
    ----------
    value : object
        Untrusted path supplied by a project or an RPC request.

    Returns
    -------
    str
        Canonical path, without empty, dot, parent or drive components.

    Raises
    ------
    ValueError
        If the path could escape the project or has an ambiguous spelling.
    """
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(ord(char) < 32 or char in "\\:" for char in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or PurePosixPath(value).is_absolute()
    ):
        raise ValueError("path must be a canonical relative POSIX project file path")
    return value


@dataclass(frozen=True)
class ProjectManifest:
    """Exact required files and patterns requiring exactly one matching file."""

    project_file: str
    required: tuple[str, ...]
    required_patterns: tuple[str, ...]

    def permits(self, path: str) -> bool:
        """Check a validated relative path against the project's file allowlist."""
        return path in self.required or any(
            matches_project_pattern(path, pattern) for pattern in self.required_patterns
        )

    def to_dict(self) -> dict[str, str | list[str]]:
        """Return the manifest in its JSON wire representation."""
        return {
            "project_file": self.project_file,
            "required": list(self.required),
            "required_patterns": list(self.required_patterns),
        }


@dataclass(frozen=True)
class ProjectComponent:
    """One software component declared by a project, as built into the firmware.

    Paths are canonical and relative to the project root. ``repository`` never
    carries URL credentials; ``revision`` is the declared source revision.
    """

    name: str
    kind: str
    scm: str | None = None
    repository: str | None = None
    revision: str | None = None
    configuration: str | None = None
    sources: str | None = None
    executables: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, str | list[str] | None]:
        """Return the component in its JSON representation."""
        return {
            "name": self.name,
            "kind": self.kind,
            "scm": self.scm,
            "repository": self.repository,
            "revision": self.revision,
            "configuration": self.configuration,
            "sources": self.sources,
            "executables": list(self.executables),
        }


@dataclass(frozen=True)
class ProjectDescription:
    """Software identity declared by a project: its name, version and components."""

    name: str | None
    version: str | None
    components: tuple[ProjectComponent, ...]

    def to_dict(self) -> dict[str, str | list[dict[str, str | list[str] | None]] | None]:
        """Return the description in its JSON representation."""
        return {
            "name": self.name,
            "version": self.version,
            "components": [component.to_dict() for component in self.components],
        }


def matches_project_pattern(path: str, pattern: str) -> bool:
    """Match every project path component, without accepting extra parent prefixes."""
    parts = PurePosixPath(path).parts
    pattern_parts = PurePosixPath(pattern).parts
    return len(parts) == len(pattern_parts) and all(
        fnmatchcase(part, expected) for part, expected in zip(parts, pattern_parts)
    )
