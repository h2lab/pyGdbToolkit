# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Named provider factories configured by the launcher, without importing vendor SDKs."""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
from pathlib import Path
import re
from typing import Protocol

from .copilot import CopilotConfig, CopilotProvider
from .provider import LlmProvider

_PROVIDER_ID = re.compile(r"[a-z][a-z0-9-]{0,63}")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ProviderConfigurationError(ValueError):
    """Launcher options cannot configure the selected provider."""


class ProviderFactory(Protocol):
    """Declare provider-specific launcher options and build a configured provider."""

    @property
    def provider_id(self) -> str:
        """Return the identifier selected with ``--llm``."""
        ...

    @property
    def description(self) -> str:
        """Return a one-line description for the launcher help."""
        ...

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare options, prefixed by the provider name, in their own group."""
        ...

    def create(self, arguments: argparse.Namespace, environ: Mapping[str, str]) -> LlmProvider:
        """Build the provider without network access, or raise ProviderConfigurationError."""
        ...


class ProviderRegistry:
    """Ordered, duplicate-free set of provider factories."""

    def __init__(self, factories: Iterable[ProviderFactory] = ()) -> None:
        """Register each factory in order."""
        self._factories: dict[str, ProviderFactory] = {}
        for factory in factories:
            self.register(factory)

    @property
    def ids(self) -> tuple[str, ...]:
        """Return the registered provider identifiers."""
        return tuple(self._factories)

    def register(self, factory: ProviderFactory) -> None:
        """Add a factory, rejecting malformed or duplicate identifiers."""
        provider_id = factory.provider_id
        if not _PROVIDER_ID.fullmatch(provider_id):
            raise ValueError(f"invalid provider identifier: {provider_id!r}")
        if provider_id in self._factories:
            raise ValueError(f"provider already registered: {provider_id}")
        self._factories[provider_id] = factory

    def get(self, provider_id: str) -> ProviderFactory:
        """Return a factory or raise ProviderConfigurationError."""
        try:
            return self._factories[provider_id]
        except KeyError:
            raise ProviderConfigurationError(f"unknown LLM provider: {provider_id}") from None

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare the options of every registered provider."""
        for factory in self._factories.values():
            factory.add_arguments(parser)

    def create(
        self, provider_id: str, arguments: argparse.Namespace, environ: Mapping[str, str]
    ) -> LlmProvider:
        """Build the selected provider from parsed options and the environment."""
        provider = self.get(provider_id).create(arguments, environ)
        if provider.provider_id != provider_id:
            raise ProviderConfigurationError(
                f"factory {provider_id} built provider {provider.provider_id}"
            )
        return provider


class CopilotProviderFactory:
    """GitHub Copilot through a pre-provisioned runtime and CLI login or an env token."""

    provider_id = CopilotProvider.provider_id
    description = "GitHub Copilot (requires the pyGdbToolkit[copilot] extra)"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare the runtime path, model and credential source options."""
        group = parser.add_argument_group("GitHub Copilot provider (--llm github-copilot)")
        group.add_argument(
            "--copilot-runtime",
            type=Path,
            metavar="PATH",
            help="Copilot runtime executable (default: $PYGDB_COPILOT_RUNTIME); never downloaded",
        )
        group.add_argument("--copilot-model", metavar="MODEL", help="Model requested from Copilot")
        group.add_argument(
            "--copilot-token-env",
            metavar="NAME",
            help="Environment variable holding a GitHub token (default: 'copilot login' account)",
        )

    def create(self, arguments: argparse.Namespace, environ: Mapping[str, str]) -> LlmProvider:
        """Resolve the runtime path and in-memory token, then build the provider."""
        runtime: Path | None = getattr(arguments, "copilot_runtime", None)
        if runtime is None and environ.get("PYGDB_COPILOT_RUNTIME"):
            runtime = Path(environ["PYGDB_COPILOT_RUNTIME"])
        if runtime is None:
            raise ProviderConfigurationError(
                "GitHub Copilot requires --copilot-runtime or PYGDB_COPILOT_RUNTIME"
            )
        token: str | None = None
        token_env: str | None = getattr(arguments, "copilot_token_env", None)
        if token_env is not None:
            if not _ENVIRONMENT_NAME.fullmatch(token_env):
                raise ProviderConfigurationError("--copilot-token-env must name a variable")
            token = environ.get(token_env, "").strip()
            if not token:
                raise ProviderConfigurationError(f"environment variable {token_env} is not set")
        try:
            config = CopilotConfig(
                runtime.expanduser().absolute(),
                model=getattr(arguments, "copilot_model", None),
                github_token=token,
            )
        except ValueError as error:
            raise ProviderConfigurationError(str(error)) from None
        return CopilotProvider(config)


def default_registry() -> ProviderRegistry:
    """Return the built-in providers; applications may register more."""
    return ProviderRegistry((CopilotProviderFactory(),))
