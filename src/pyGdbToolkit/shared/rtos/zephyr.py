# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Zephyr build identity and transfer paths, independent of GDB and source access."""

from dataclasses import dataclass
from pathlib import PurePosixPath

import yaml

from .models import ProjectComponent, ProjectDescription, ProjectManifest

NAME = "zephyr"
PROJECT_FILE = "build_info.yml"
KERNEL_ELF = "zephyr/zephyr.elf"
KCONFIG = "zephyr/.config"
DEVICETREE = "zephyr/zephyr.dts"
REQUIRED = (PROJECT_FILE, KERNEL_ELF, KCONFIG, DEVICETREE)
MAX_METADATA_SIZE = 1024 * 1024


def _mapping(value: object, where: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{PROJECT_FILE}: {where} must be a mapping with string keys")
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _text(value: object, where: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"{PROJECT_FILE}: {where} must be a nonempty printable string")
    return value


@dataclass(frozen=True)
class ZephyrBuild:
    """Build identity; original compilation paths are metadata, not trusted read roots."""

    application: str
    application_source: str
    zephyr_base: str
    version: str
    board: str
    qualifiers: str

    def description(self) -> ProjectDescription:
        """Describe application and kernel without exposing absolute source paths.

        The shared component contract only accepts project-relative source paths.
        A build directory generally contains neither application's nor kernel's sources;
        source-root authorization is separate from build artifact loading.
        """
        return ProjectDescription(
            name=self.application,
            version=None,
            components=(
                ProjectComponent(self.application, "application", executables=(KERNEL_ELF,)),
                ProjectComponent(
                    NAME,
                    "kernel",
                    revision=self.version,
                    configuration=KCONFIG,
                    executables=(KERNEL_ELF,),
                ),
            ),
        )


def parse_build_info(content: bytes) -> ZephyrBuild:
    """Read the identity of a single-image Zephyr build without following paths.

    Parameters
    ----------
    content : bytes
        Generated build_info.yml, whose schema version must be 0.1.0.

    Returns
    -------
    ZephyrBuild
        Application, board and kernel identity from the generated build metadata.

    Raises
    ------
    ValueError
        If the YAML, schema version or required identity fields are invalid.
    """
    if len(content) > MAX_METADATA_SIZE:
        raise ValueError(f"{PROJECT_FILE}: exceeds {MAX_METADATA_SIZE} bytes")
    try:
        data: object = yaml.safe_load(content.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError, RecursionError) as error:
        raise ValueError(f"{PROJECT_FILE}: invalid UTF-8 YAML") from error
    root = _mapping(data, "document")
    if root.get("version") != "0.1.0":
        raise ValueError(f"{PROJECT_FILE}: unsupported schema version")
    cmake = _mapping(root.get("cmake"), "cmake")
    application = _mapping(cmake.get("application"), "cmake.application")
    board = _mapping(cmake.get("board"), "cmake.board")
    zephyr = _mapping(cmake.get("zephyr"), "cmake.zephyr")
    source = _text(application.get("source-dir"), "cmake.application.source-dir")
    name = PurePosixPath(source).name
    if not name or name in {".", ".."} or "\\" in source:
        raise ValueError(f"{PROJECT_FILE}: invalid application source directory")
    qualifiers = board.get("qualifiers", "")
    if not isinstance(qualifiers, str) or any(
        ord(char) < 32 or ord(char) == 127 for char in qualifiers
    ):
        raise ValueError(f"{PROJECT_FILE}: invalid board qualifiers")
    return ZephyrBuild(
        application=name,
        application_source=source,
        zephyr_base=_text(zephyr.get("zephyr-base"), "cmake.zephyr.zephyr-base"),
        version=_text(zephyr.get("version"), "cmake.zephyr.version"),
        board=_text(board.get("name"), "cmake.board.name"),
        qualifiers=qualifiers,
    )


class ZephyrProjectProvider:
    """Declare a single-image build's final artifacts, never workspace-wide globs."""

    name = NAME

    def manifest(self, project: bytes | None = None) -> ProjectManifest:
        """Return bootstrap or complete artifact paths rooted at the build directory."""
        if project is None:
            return ProjectManifest(PROJECT_FILE, (PROJECT_FILE,), ())
        parse_build_info(project)
        return ProjectManifest(PROJECT_FILE, REQUIRED, ())

    def describe(self, project: bytes) -> ProjectDescription:
        """Return application and Zephyr identity for the client audit context."""
        return parse_build_info(project).description()
