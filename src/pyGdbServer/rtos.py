# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Bounded, isolated staging of RTOS projects transferred over JSON-RPC."""

import base64
import binascii
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import re
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import RLock
from typing import Literal

from pyGdbToolkit.shared.rtos import (
    DEFAULT_PROJECT_REGISTRY,
    ProjectManifest,
    ProjectRegistry,
    matches_project_pattern,
    project_relative_path,
)

CHUNK_SIZE = 4 * 1024 * 1024
FILE_SIZE = 128 * 1024 * 1024
PROJECT_SIZE = 512 * 1024 * 1024
PROJECT_FILE_SIZE = 1024 * 1024
MAX_FILES = 256
MAX_PROJECTS = 16


@dataclass
class _FileUpload:
    size: int
    temporary: Path
    complete: bool = False


@dataclass
class _Project:
    rtos: str
    temporary: TemporaryDirectory[str]
    manifest: ProjectManifest
    files: dict[str, _FileUpload] = field(default_factory=dict)
    state: Literal["staging", "loading", "loaded", "failed"] = "staging"

    @property
    def root(self) -> Path:
        return Path(self.temporary.name) / "project"


def _name(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value) is None:
        raise ValueError("name must contain 1-64 ASCII letters, digits, underscores or hyphens")
    return value


def _integer(value: object, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{label} must be an integer between 0 and {maximum}")
    return value


class RtosProjects:
    """Own uploaded projects for one server lifetime, independently of GDB.

    A lock protects staging from concurrent connections and worker threads.
    Loaded projects remain immutable and available until GDB stops.
    """

    def __init__(
        self, workspace: Path, registry: ProjectRegistry = DEFAULT_PROJECT_REGISTRY
    ) -> None:
        """Bind staging to the server workspace without creating any files."""
        self.workspace = workspace
        self._registry = registry
        self._projects: dict[str, _Project] = {}
        self._lock = RLock()

    def begin(self, name: object, rtos: object) -> dict[str, object]:
        """Allocate an isolated project and return its bootstrap manifest and limits.

        Parameters
        ----------
        name : object
            Unique transfer name within this server instance.
        rtos : object
            Supported RTOS identifier.

        Returns
        -------
        dict[str, object]
            Transfer name, bootstrap manifest and byte/count limits.

        Raises
        ------
        ValueError
            If inputs are invalid, the name exists or the transfer limit is reached.
        """
        name = _name(name)
        if not isinstance(rtos, str):
            raise ValueError("rtos must be a string")
        manifest = self._registry.manifest(rtos)
        with self._lock:
            if name in self._projects:
                raise ValueError("project name already exists; abort it or use a new name")
            if len(self._projects) >= MAX_PROJECTS:
                raise ValueError(f"at most {MAX_PROJECTS} projects may be retained")
            temporary = TemporaryDirectory(prefix="pygdb-rtos-", dir=self.workspace)
            project = _Project(rtos, temporary, manifest)
            try:
                project.root.mkdir()
                (Path(temporary.name) / "chunks").mkdir()
            except OSError:
                temporary.cleanup()
                raise
            self._projects[name] = project
        return {
            "name": name,
            "rtos": rtos,
            "manifest": manifest.to_dict(),
            "limits": {
                "chunk_size": CHUNK_SIZE,
                "file_size": FILE_SIZE,
                "project_size": PROJECT_SIZE,
                "project_file_size": PROJECT_FILE_SIZE,
                "files": MAX_FILES,
                "projects": MAX_PROJECTS,
            },
        }

    def _project(self, name: object) -> _Project:
        name = _name(name)
        if name not in self._projects:
            raise ValueError(f"Unknown project transfer: {name}")
        return self._projects[name]

    @staticmethod
    def _path(project: _Project, relative: str) -> Path:
        root = project.root
        destination = root / relative
        if (
            not destination.resolve().is_relative_to(root.resolve())
            or any(candidate.is_symlink() for candidate in (root.parent, root, destination))
            or any(
                (root / Path(*PurePosixPath(relative).parts[:index])).is_symlink()
                for index in range(1, len(PurePosixPath(relative).parts))
            )
        ):
            raise ValueError("project file must remain inside staging without symbolic links")
        return destination

    def put(
        self, name: object, path: object, offset: object, size: object, content: object
    ) -> dict[str, object]:
        """Append a strict base64 chunk at the next contiguous byte offset.

        Parameters
        ----------
        name : object
            Transfer name returned by ``begin``.
        path : object
            Manifest-approved relative POSIX file path.
        offset : object
            Next byte offset, starting at zero.
        size : object
            Total decoded file size, identical on every chunk.
        content : object
            Strict base64-encoded bytes, at most ``CHUNK_SIZE`` before encoding.

        Returns
        -------
        dict[str, object]
            File path, next offset, completion flag and current manifest.

        Raises
        ------
        ValueError
            If a path, size, offset, manifest or transfer state is invalid.
        OSError
            If staging cannot be written.
        """
        relative = project_relative_path(path)
        offset = _integer(offset, "offset", FILE_SIZE)
        size = _integer(size, "size", FILE_SIZE)
        if not isinstance(content, str) or len(content) > 4 * ((CHUNK_SIZE + 2) // 3):
            raise ValueError(f"content must be base64 encoding at most {CHUNK_SIZE} bytes")
        try:
            data = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("content must be valid base64") from error
        if len(data) > CHUNK_SIZE or offset + len(data) > size or (not data and size != 0):
            raise ValueError("chunk must be nonempty and fit within the declared file size")
        with self._lock:
            project = self._project(name)
            if relative == project.manifest.project_file:
                _integer(size, "size", PROJECT_FILE_SIZE)
            if project.state != "staging":
                raise ValueError("project files are frozen once loading starts")
            if not project.manifest.permits(relative):
                raise ValueError(
                    f"path is not in the manifest; upload {project.manifest.project_file} first"
                )
            destination = self._path(project, relative)
            upload = project.files.get(relative)
            if upload is None:
                if offset != 0:
                    raise ValueError("first chunk offset must be zero")
                if len(project.files) >= MAX_FILES:
                    raise ValueError(f"project must not exceed {MAX_FILES} files")
                if sum(item.size for item in project.files.values()) + size > PROJECT_SIZE:
                    raise ValueError(f"project must not exceed {PROJECT_SIZE} bytes")
                with NamedTemporaryFile(
                    dir=Path(project.temporary.name) / "chunks", delete=False
                ) as temporary:
                    upload = _FileUpload(size, Path(temporary.name))
                project.files[relative] = upload
            if upload.complete:
                raise ValueError("file is already complete and cannot be overwritten")
            if size != upload.size or offset != upload.temporary.stat().st_size:
                raise ValueError("size must remain constant and offset must equal the next byte")
            with upload.temporary.open("ab") as output:
                output.write(data)
            next_offset = upload.temporary.stat().st_size
            if next_offset == size:
                manifest = project.manifest
                if relative == project.manifest.project_file:
                    try:
                        manifest = self._registry.manifest(
                            project.rtos, upload.temporary.read_bytes()
                        )
                    except ValueError:
                        upload.temporary.unlink()
                        del project.files[relative]
                        raise
                destination.parent.mkdir(parents=True, exist_ok=True)
                upload.temporary.replace(destination)
                upload.complete = True
                project.manifest = manifest
            return {
                "path": relative,
                "next_offset": next_offset,
                "complete": upload.complete,
                "manifest": project.manifest.to_dict(),
            }

    def prepare(self, name: object) -> tuple[str, Path]:
        """Validate all declared files before freezing a project for GDB activation."""
        with self._lock:
            project = self._project(name)
            if project.state not in {"staging", "failed"}:
                raise ValueError("project is already loading or loaded")
            incomplete = [path for path, upload in project.files.items() if not upload.complete]
            missing = set(project.manifest.required) - project.files.keys()
            if missing or incomplete:
                raise ValueError(
                    f"project is incomplete; missing: {sorted(missing)}, incomplete: {incomplete}"
                )
            for pattern in project.manifest.required_patterns:
                matches = [path for path in project.files if matches_project_pattern(path, pattern)]
                if len(matches) != 1:
                    raise ValueError(f"project requires exactly one file matching {pattern}")
            for path, upload in project.files.items():
                source = self._path(project, path)
                if not source.is_file() or source.stat().st_size != upload.size:
                    raise ValueError(f"staged file is missing or changed: {path}")
            project.state = "loading"
            return project.rtos, project.root

    def finish(self, name: object, success: bool) -> None:
        """Record activation outcome; failed loads stay frozen but can be retried."""
        with self._lock:
            self._project(name).state = "loaded" if success else "failed"

    def abort(self, name: object) -> dict[str, object]:
        """Remove an unfinished upload, never files that GDB might still reference."""
        with self._lock:
            project = self._project(name)
            if project.state != "staging":
                raise ValueError("only a staging project can be aborted; GDB may reference files")
            project.temporary.cleanup()
            del self._projects[_name(name)]
            return {"name": name, "aborted": True}

    def close(self) -> None:
        """Remove this store's isolated directories after GDB has stopped."""
        with self._lock:
            for project in self._projects.values():
                project.temporary.cleanup()
            self._projects.clear()
