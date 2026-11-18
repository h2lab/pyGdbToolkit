# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Explicit client workspace authorization for build-declared Zephyr source trees."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from pyGdbToolkit.shared.rtos.zephyr import parse_build_info


@dataclass(frozen=True)
class SourceRoot:
    """One alias, workspace-relative directory and bound directory identity."""

    alias: str
    directory: str
    device: int
    inode: int


@dataclass(frozen=True)
class SourceWorkspace:
    """Auditor-selected boundary; metadata declares roots but cannot enlarge it."""

    path: Path
    device: int
    inode: int
    roots: tuple[SourceRoot, ...]
    metadata: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, object]:
        """Return source authorization and provenance, not a firmware/source equality claim."""
        return {
            "workspace": str(self.path),
            "roots": [{"alias": root.alias, "directory": root.directory} for root in self.roots],
            "metadata_sha256": dict(self.metadata),
            "revision_status": "local source revisions and contents are not verified against ELF",
        }


def discover_sources(root: Path, workspace: Path, declaration: bytes) -> SourceWorkspace:
    """Authorize only declared directories inside a user-selected Zephyr workspace.

    Parameters
    ----------
    root : Path
        Local build directory containing build_info.yml and zephyr_modules.txt.
    workspace : Path
        Explicitly authorized local workspace. It must contain build and source directories.
    declaration : bytes
        Same build_info.yml bytes validated and hashed by the project transfer.

    Returns
    -------
    SourceWorkspace
        Bounded roots with directory identities and hashes of discovery inputs.

    Raises
    ------
    ValueError
        If metadata, paths or directory identities cannot be safely validated.
    """
    from .llm.sources import SourceBrowser, SourceError

    boundary = workspace.expanduser().absolute()
    # Do not resolve links in the authorized boundary or its ancestors.
    if ".." in boundary.parts or any(
        parent.is_symlink() for parent in (boundary, *boundary.parents)
    ):
        raise ValueError("source workspace must not contain parent traversal or symbolic links")
    browser = SourceBrowser(boundary, nofollow_ancestors=True)
    if boundary.parent == boundary:
        raise ValueError("filesystem root cannot be a source workspace")
    try:
        build_relative = root.relative_to(boundary).as_posix()
    except ValueError as error:
        raise ValueError(
            "build directory must be inside the authorized source workspace"
        ) from error
    if build_relative == ".":
        raise ValueError("source workspace must be an ancestor of the build directory")
    if browser.read_bytes(f"{build_relative}/build_info.yml") != declaration:
        raise ValueError("build_info.yml changed during source discovery")
    content = browser.read_bytes(f"{build_relative}/zephyr_modules.txt")
    build = parse_build_info(declaration)
    declared = [
        ("build", str(root)),
        ("application", build.application_source),
        ("zephyr", build.zephyr_base),
    ]
    try:
        lines = content.decode("utf-8").splitlines()
    except UnicodeError as error:
        raise ValueError("zephyr_modules.txt must be UTF-8") from error
    if len(lines) > 256:
        raise ValueError("zephyr_modules.txt exceeds 256 modules")
    for line in lines:
        if not line:
            continue
        match = re.fullmatch(r'("(?:[^"\\]|\\.)*"):("(?:[^"\\]|\\.)*"):("(?:[^"\\]|\\.)*")', line)
        if match is None:
            raise ValueError("invalid zephyr_modules.txt record; expected three quoted fields")
        try:
            name, directory, _ = (json.loads(value) for value in match.groups())
        except ValueError as error:
            raise ValueError("invalid quoted field in zephyr_modules.txt") from error
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", name) is None:
            raise ValueError("invalid Zephyr module name")
        declared.append((f"module-{name}", directory))
    roots = []
    aliases: set[str] = set()
    for alias, directory in declared:
        if alias in aliases:
            raise ValueError(f"duplicate Zephyr source alias: {alias}")
        aliases.add(alias)
        path = Path(directory)
        if not path.is_absolute():
            raise ValueError(f"Zephyr source directory must be absolute: {alias}")
        try:
            relative = path.relative_to(boundary).as_posix()
        except ValueError as error:
            raise ValueError(f"Zephyr source root outside authorized workspace: {alias}") from error
        if relative == ".":
            raise ValueError(f"Zephyr source root may not expose the entire workspace: {alias}")
        try:
            identity = browser.directory_identity(relative)
        except SourceError as error:
            raise ValueError(f"cannot authorize Zephyr source root {alias}: {error}") from error
        roots.append(SourceRoot(alias, relative, *identity))
    return SourceWorkspace(
        boundary,
        *browser.directory_identity("."),
        tuple(roots),
        (
            ("build_info.yml", hashlib.sha256(declaration).hexdigest()),
            ("zephyr_modules.txt", hashlib.sha256(content).hexdigest()),
        ),
    )
