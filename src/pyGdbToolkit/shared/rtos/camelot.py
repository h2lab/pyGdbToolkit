# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Camelot build discovery shared by GDB and remote project transfers."""

from collections.abc import Mapping
import tomllib

from .models import ProjectComponent, ProjectDescription, ProjectManifest, project_relative_path

NAME = "camelot"
PROJECT_FILE = "project.toml"
PRIVATE_BUILD = "output/build/camelot_private"
KERNEL_ELF = f"{PRIVATE_BUILD}/sentry-kernel.elf"
SOURCES = "output/src"
KCONFIG_PATTERN = "output/build/kernel/subprojects/kconfig-*/generated_kconfig.json"


def _application_elf(name: object, application: object) -> str:
    """Return the relinked ELF of one application, named by its first ``provides`` entry.

    Barbican relinks only the first provided executable of an application into
    the private build directory, keeping its file name.
    """
    if not isinstance(name, str) or not isinstance(application, dict):
        raise ValueError("project.toml: application entries must be tables")
    provides = application.get("provides")
    if not isinstance(provides, list) or not provides:
        raise ValueError(f"project.toml: application {name} must declare a provides list")
    executable = provides[0]
    if not isinstance(executable, str) or not executable or "/" in executable:
        raise ValueError(f"project.toml: application {name} provides must be file names")
    return project_relative_path(f"{PRIVATE_BUILD}/{executable}")


def project_elf_paths(project: Mapping[str, object]) -> tuple[str, ...]:
    """Return relative linked ELF paths declared by a parsed Camelot project."""
    elfs = []
    if "kernel" in project:
        elfs.append(KERNEL_ELF)
    applications = project.get("application", {})
    if not isinstance(applications, dict):
        raise ValueError("project.toml: application must be a table")
    for name, application in applications.items():
        elfs.append(_application_elf(name, application))
    if not elfs:
        raise ValueError("project.toml: no kernel or application declared")
    if len(set(elfs)) != len(elfs):
        raise ValueError("project.toml: applications must provide distinct ELF files")
    return tuple(elfs)


def _optional_string(table: Mapping[str, object], key: str, where: str) -> str | None:
    value = table.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"project.toml: {where}.{key} must be a string")
    return value


def _without_credentials(uri: str) -> str:
    """Drop the user information, query and fragment of a URL, which may hold tokens."""
    scheme, separator, rest = uri.partition("://")
    if not separator:
        return uri
    authority, slash, path = rest.partition("/")
    path = path.partition("?")[0].partition("#")[0]
    authority = authority.partition("?")[0].partition("#")[0]
    return f"{scheme}://{authority.rpartition('@')[2]}{slash}{path}"


def _component(
    name: str, kind: str, table: object, executables: tuple[str, ...]
) -> ProjectComponent:
    """Describe one declared kernel, runtime or application package."""
    if not isinstance(table, dict):
        raise ValueError(f"project.toml: {name} must be a table")
    if not name or "/" in name or "\\" in name:
        raise ValueError(f"project.toml: invalid component name {name!r}")
    scm = table.get("scm")
    scm_kind = repository = revision = None
    if scm is not None:
        if not isinstance(scm, dict) or len(scm) != 1:
            raise ValueError(f"project.toml: {name}.scm must declare exactly one source")
        scm_kind, origin = next(iter(scm.items()))
        if not isinstance(origin, dict):
            raise ValueError(f"project.toml: {name}.scm.{scm_kind} must be a table")
        uri = _optional_string(origin, "uri", f"{name}.scm.{scm_kind}")
        repository = None if uri is None else _without_credentials(uri)
        revision = _optional_string(origin, "revision", f"{name}.scm.{scm_kind}")
    return ProjectComponent(
        name,
        kind,
        scm_kind,
        repository,
        revision,
        _optional_string(table, "config", name),
        project_relative_path(f"{SOURCES}/{name}"),
        executables,
    )


def project_description(project: Mapping[str, object]) -> ProjectDescription:
    """Return the software identity declared by a parsed Camelot project.

    Parameters
    ----------
    project : Mapping[str, object]
        Parsed ``project.toml`` contents.

    Returns
    -------
    ProjectDescription
        Project name and version, then the kernel, runtime and applications
        with their declared sources, revisions and linked ELF files.

    Raises
    ------
    ValueError
        If a component declaration is malformed.
    """
    components = []
    if "kernel" in project:
        components.append(_component("kernel", "kernel", project["kernel"], (KERNEL_ELF,)))
    if "runtime" in project:
        components.append(_component("runtime", "runtime", project["runtime"], ()))
    applications = project.get("application", {})
    if not isinstance(applications, dict):
        raise ValueError("project.toml: application must be a table")
    for name, application in applications.items():
        components.append(
            _component(name, "application", application, (_application_elf(name, application),))
        )
    return ProjectDescription(
        _optional_string(project, "name", "project"),
        _optional_string(project, "version", "project"),
        tuple(components),
    )


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
        elfs: tuple[str, ...] = (KERNEL_ELF,)
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

    def describe(self, project: bytes) -> ProjectDescription:
        """Return the software identity declared by a ``project.toml``.

        Parameters
        ----------
        project : bytes
            Completed ``project.toml`` contents.

        Returns
        -------
        ProjectDescription
            Declared name, version and components.

        Raises
        ------
        ValueError
            If the declaration is not valid UTF-8 TOML or is malformed.
        """
        return project_description(tomllib.loads(project.decode("utf-8")))
