# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Camelot build discovery shared by GDB and remote project transfers."""

from collections.abc import Mapping
import tomllib

from .models import ProjectManifest, project_relative_path

NAME = "camelot"
PROJECT_FILE = "project.toml"
PRIVATE_BUILD = "output/build/camelot_private"
KCONFIG_PATTERN = "output/build/kernel/subprojects/kconfig-*/generated_kconfig.json"


def project_elf_paths(project: Mapping[str, object]) -> tuple[str, ...]:
    """Return relative linked ELF paths declared by a parsed Camelot project."""
    elfs = []
    if "kernel" in project:
        elfs.append(f"{PRIVATE_BUILD}/sentry-kernel.elf")
    applications = project.get("application", {})
    if not isinstance(applications, dict):
        raise ValueError("project.toml: application must be a table")
    for name in applications:
        if not isinstance(name, str) or "/" in name:
            raise ValueError("project.toml: application names must be file names")
        elfs.append(project_relative_path(f"{PRIVATE_BUILD}/{name}-app.elf"))
    if not elfs:
        raise ValueError("project.toml: no kernel or application declared")
    return tuple(elfs)


class CamelotProjectProvider:
    """Describe the build artifacts required by Camelot's GDB implementation."""

    name = NAME

    def manifest(self, project: bytes | None = None) -> ProjectManifest:
        """Return bootstrap paths or the complete manifest from project.toml.

        Parameters
        ----------
        project : bytes | None
            Completed ``project.toml`` contents, or None for bootstrap discovery.

        Returns
        -------
        ProjectManifest
            Required Camelot artifacts and the generated kernel Kconfig pattern.

        Raises
        ------
        ValueError
            If the declaration is invalid or does not include a kernel.
        """
        elfs: tuple[str, ...] = (f"{PRIVATE_BUILD}/sentry-kernel.elf",)
        if project is not None:
            parsed = tomllib.loads(project.decode("utf-8"))
            if "kernel" not in parsed:
                raise ValueError("Cannot load RTOS task metadata: kernel ELF is required")
            elfs = project_elf_paths(parsed)
        return ProjectManifest(
            PROJECT_FILE,
            (
                PROJECT_FILE,
                *elfs,
                f"{PRIVATE_BUILD}/layout.json",
                "output/build/kernel/kernel/include/sentry/managers/task_metadata.h",
            ),
            (KCONFIG_PATTERN,),
        )
