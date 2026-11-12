# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Bounded, read-only access to the auditor's explicitly loaded project."""

from contextlib import contextmanager
from collections.abc import Iterator
from fnmatch import fnmatchcase
from functools import lru_cache
import os
from pathlib import Path
import stat
import time

from .contracts import JsonObject, JsonValue

MAX_FILE_BYTES = 256 * 1024
MAX_SCAN_BYTES = 16 * 1024 * 1024
MAX_SCAN_ENTRIES = 20000
MAX_DEPTH = 24
MAX_RESULTS = 100
MAX_TEXT_CHARS = 12000
MAX_SCAN_SECONDS = 3.0


class SourceError(ValueError):
    """A project source operation was refused or could not be completed."""


def _parts(path: str, *, root: bool = False) -> tuple[str, ...]:
    if path == "." and root:
        return ()
    parts = tuple(path.split("/"))
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or len(parts) > MAX_DEPTH
        or any(not part or part in (".", "..") or part.startswith(".") for part in parts)
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    ):
        raise SourceError("path must be relative to the project and contain no hidden components")
    return parts


def validate_source_arguments(name: str, arguments: JsonObject) -> None:
    """Reject unsafe paths and patterns before any step in a plan is executed."""
    path = arguments.get("path")
    if not isinstance(path, str):
        raise SourceError("path must be a string")
    _parts(path, root=name != "read")
    glob = arguments.get("pattern") if name == "glob" else arguments.get("glob")
    if glob is not None:
        if not isinstance(glob, str):
            raise SourceError("glob must be a string")
        _parts(glob)


@lru_cache(maxsize=4096)
def _matches(path: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if not pattern:
        return not path
    if pattern[0] == "**":
        return _matches(path, pattern[1:]) or bool(path and _matches(path[1:], pattern))
    return bool(path and fnmatchcase(path[0], pattern[0]) and _matches(path[1:], pattern[1:]))


class SourceBrowser:
    """Access one loaded project without following links or opening special files.

    Linux ``O_PATH`` and procfs are required to inspect a file before opening it
    for reading, including if another process replaces its directory entry.
    """

    def __init__(self, root: Path) -> None:
        """Bind the project directory identity for the lifetime of a conversation."""
        if not hasattr(os, "O_PATH") or not hasattr(os, "O_NOFOLLOW"):
            raise SourceError("secure source access requires Linux O_PATH and O_NOFOLLOW")
        self._root = root
        try:
            with self._directory((), check_identity=False) as fd:
                info = os.fstat(fd)
                self._identity = (info.st_dev, info.st_ino)
        except OSError as error:
            raise SourceError(f"cannot open project directory: {error.strerror}") from error

    @contextmanager
    def _directory(self, parts: tuple[str, ...], *, check_identity: bool = True) -> Iterator[int]:
        fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if check_identity and (info.st_dev, info.st_ino) != self._identity:
                raise SourceError("project directory changed; open a new agent session")
            for part in parts:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    def _file(self, parts: tuple[str, ...]) -> bytes:
        with self._directory(parts[:-1]) as directory:
            handle = os.open(parts[-1], os.O_PATH | os.O_NOFOLLOW, dir_fd=directory)
            try:
                info = os.fstat(handle)
                if not stat.S_ISREG(info.st_mode):
                    raise SourceError("only regular files are readable; links are not followed")
                if info.st_size > MAX_FILE_BYTES:
                    raise SourceError(f"file exceeds {MAX_FILE_BYTES} bytes")
                fd = os.open(f"/proc/self/fd/{handle}", os.O_RDONLY | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as stream:
                    data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise SourceError(f"file exceeds {MAX_FILE_BYTES} bytes")
                return data
            finally:
                os.close(handle)

    def execute(self, name: str, arguments: JsonObject) -> JsonObject:
        """Execute a validated source call and return bounded JSON data."""
        try:
            path = arguments["path"]
            if not isinstance(path, str):
                raise SourceError("path must be a string")
            if name == "read":
                parts = _parts(path)
                data = self._file(parts)
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError as error:
                    raise SourceError("file is not UTF-8 text") from error
                if "\0" in text:
                    raise SourceError("file is not UTF-8 text")
                offset, limit = arguments["offset"], arguments["limit"]
                if not isinstance(offset, int) or not isinstance(limit, int):
                    raise SourceError("offset and limit must be integers")
                lines = text.splitlines()
                selected = lines[offset - 1 : offset - 1 + limit]
                content = "\n".join(
                    f"{offset + index}: {line}" for index, line in enumerate(selected)
                )
                return {
                    "path": path,
                    "content": content[:MAX_TEXT_CHARS],
                    "truncated": len(content) > MAX_TEXT_CHARS or offset - 1 + limit < len(lines),
                    "total_lines": len(lines),
                }
            if name not in ("glob", "grep"):
                raise SourceError("unknown source operation")
            directory = _parts(path, root=True)
            pattern = arguments.get("pattern")
            if not isinstance(pattern, str):
                raise SourceError("pattern must be a string")
            glob = pattern if name == "glob" else arguments.get("glob", "**")
            if not isinstance(glob, str):
                raise SourceError("glob must be a string")
            glob_parts = _parts(glob)
            return self._scan(directory, glob_parts, pattern if name == "grep" else None)
        except OSError as error:
            raise SourceError(f"source access failed: {error.strerror}") from error

    def _scan(
        self, directory: tuple[str, ...], pattern: tuple[str, ...], search: str | None
    ) -> JsonObject:
        started = time.monotonic()
        entries = 0
        scanned_bytes = 0
        skipped = 0
        truncated = False
        matches: list[JsonValue] = []
        stack: list[tuple[str, ...]] = [()]
        while stack and not truncated:
            relative = stack.pop()
            with self._directory(directory + relative) as fd:
                with os.scandir(fd) as listing:
                    for entry in listing:
                        entries += 1
                        if (
                            entries > MAX_SCAN_ENTRIES
                            or time.monotonic() - started > MAX_SCAN_SECONDS
                        ):
                            truncated = True
                            break
                        if entry.name.startswith("."):
                            continue
                        child = relative + (entry.name,)
                        if len(directory) + len(child) > MAX_DEPTH:
                            truncated = True
                            break
                        if entry.is_dir(follow_symlinks=False):
                            if len(directory) + len(child) < MAX_DEPTH:
                                stack.append(child)
                            else:
                                truncated = True
                        elif entry.is_file(follow_symlinks=False) and _matches(child, pattern):
                            path = "/".join(directory + child)
                            if search is None:
                                matches.append({"path": path})
                            else:
                                try:
                                    data = self._file(directory + child)
                                except SourceError as error:
                                    if str(error).startswith("file exceeds "):
                                        skipped += 1
                                        continue
                                    raise
                                scanned_bytes += len(data)
                                if scanned_bytes > MAX_SCAN_BYTES:
                                    truncated = True
                                    break
                                try:
                                    text = data.decode("utf-8")
                                except UnicodeDecodeError:
                                    skipped += 1
                                    continue
                                if "\0" in text:
                                    skipped += 1
                                    continue
                                for number, line in enumerate(text.splitlines(), 1):
                                    if search in line:
                                        matches.append(
                                            {"path": path, "line": number, "text": line[:512]}
                                        )
                                        if len(matches) >= MAX_RESULTS:
                                            break
                            if len(matches) >= MAX_RESULTS:
                                truncated = True
                                break
        return {
            "matches": matches,
            "truncated": truncated,
            "skipped_non_text_or_large": skipped,
            "entries_scanned": entries,
        }
