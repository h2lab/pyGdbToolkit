# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""RTOS project provider registry, independent of GDB and file transfer mechanisms."""

from collections.abc import Sequence
from typing import Protocol

from .camelot import CamelotProjectProvider
from .models import ProjectManifest


class ProjectProvider(Protocol):
    """Describe an RTOS's project declaration and required build artifacts."""

    @property
    def name(self) -> str:
        """Return the RTOS identifier used by its GDB implementation."""
        ...

    def manifest(self, project: bytes | None = None) -> ProjectManifest:
        """Return bootstrap or expanded paths for a project declaration."""
        ...


class ProjectRegistry:
    """Dispatch manifest discovery to registered RTOS providers."""

    def __init__(self, providers: Sequence[ProjectProvider] = ()) -> None:
        """Register providers, rejecting duplicate identifiers."""
        self._providers: dict[str, ProjectProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: ProjectProvider) -> None:
        """Register one provider without silently replacing an existing implementation."""
        if not provider.name or provider.name in self._providers:
            raise ValueError(f"Invalid or duplicate RTOS project provider: {provider.name}")
        self._providers[provider.name] = provider

    def manifest(self, rtos: str, project: bytes | None = None) -> ProjectManifest:
        """Resolve the RTOS and return its bootstrap or expanded manifest.

        Parameters
        ----------
        rtos : str
            Identifier of a registered RTOS provider.
        project : bytes | None
            Completed project declaration, or None for bootstrap discovery.

        Returns
        -------
        ProjectManifest
            Provider-specific declaration path and build artifacts.

        Raises
        ------
        ValueError
            If the RTOS is unsupported or the provider rejects the declaration.
        """
        provider = self._providers.get(rtos)
        if provider is None:
            raise ValueError(f"Unsupported RTOS: {rtos}")
        return provider.manifest(project)


DEFAULT_PROJECT_REGISTRY = ProjectRegistry((CamelotProjectProvider(),))


def project_manifest(rtos: str, project: bytes | None = None) -> ProjectManifest:
    """Discover a project's manifest using the default RTOS provider registry."""
    return DEFAULT_PROJECT_REGISTRY.manifest(rtos, project)
