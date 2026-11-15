# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Named provider factories configured by the launcher, without importing vendor SDKs."""

import argparse
from collections.abc import Iterable, Mapping
import os
from pathlib import Path
import re
import stat
from typing import Protocol

from .copilot import CopilotConfig, CopilotProvider
from .opencode import OpenCodeConfig, OpenCodeProvider
from .openai_compat import OpenAiConfig, OpenAiProvider
from .provider import LlmProvider

_PROVIDER_ID = re.compile(r"[a-z][a-z0-9-]{0,63}")
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MAX_PASSWORD_FILE_BYTES = 16 * 1024


class ProviderConfigurationError(ValueError):
    """Launcher options cannot configure the selected provider."""


def _read_opencode_password(path: Path) -> str:
    try:
        fd = os.open(path.expanduser(), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ProviderConfigurationError(
                    "--opencode-password-file must name a regular file"
                )
            data = source.read(_MAX_PASSWORD_FILE_BYTES + 1)
    except ProviderConfigurationError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise ProviderConfigurationError("cannot read --opencode-password-file") from error
    if len(data) > _MAX_PASSWORD_FILE_BYTES:
        raise ProviderConfigurationError("--opencode-password-file exceeds 16384 bytes")
    try:
        password = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ProviderConfigurationError("--opencode-password-file must contain UTF-8") from None
    if password.endswith("\n"):
        password = password[:-1].removesuffix("\r")
    if not password.strip() or any(character in password for character in "\r\n\0"):
        raise ProviderConfigurationError(
            "--opencode-password-file must contain one nonempty line without NUL characters"
        )
    return password


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


class OpenAiProviderFactory:
    """OpenAI-compatible endpoint, such as an Ollama or llama.cpp server on the LAN."""

    provider_id = OpenAiProvider.provider_id
    description = "OpenAI-compatible endpoint (requires the pyGdbToolkit[openai] extra)"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare the endpoint, model, credential source and timeout options."""
        group = parser.add_argument_group("OpenAI-compatible provider (--llm openai)")
        group.add_argument(
            "--openai-url",
            metavar="URL",
            help=(
                "Base URL of the /v1 API, e.g. http://ollama.lan:11434/v1 "
                "(default: $PYGDB_OPENAI_URL)"
            ),
        )
        group.add_argument(
            "--openai-model",
            metavar="MODEL",
            help="Model served by the endpoint (default: $PYGDB_OPENAI_MODEL)",
        )
        group.add_argument(
            "--openai-key-env",
            metavar="NAME",
            help="Environment variable holding the API key (default: anonymous access)",
        )
        group.add_argument(
            "--openai-timeout",
            type=float,
            metavar="SECONDS",
            help="Timeout of each request or stream read (default: 120)",
        )

    def create(self, arguments: argparse.Namespace, environ: Mapping[str, str]) -> LlmProvider:
        """Resolve the endpoint, model and in-memory key, then build the provider."""
        url: str | None = getattr(arguments, "openai_url", None)
        if url is None and environ.get("PYGDB_OPENAI_URL", "").strip():
            url = environ["PYGDB_OPENAI_URL"].strip()
        if url is None:
            raise ProviderConfigurationError(
                "the OpenAI-compatible provider requires --openai-url or PYGDB_OPENAI_URL"
            )
        model: str | None = getattr(arguments, "openai_model", None)
        if model is None and environ.get("PYGDB_OPENAI_MODEL", "").strip():
            model = environ["PYGDB_OPENAI_MODEL"].strip()
        if model is None:
            raise ProviderConfigurationError(
                "the OpenAI-compatible provider requires --openai-model or PYGDB_OPENAI_MODEL"
            )
        key: str | None = None
        key_env: str | None = getattr(arguments, "openai_key_env", None)
        if key_env is not None:
            if not _ENVIRONMENT_NAME.fullmatch(key_env):
                raise ProviderConfigurationError("--openai-key-env must name a variable")
            key = environ.get(key_env, "").strip()
            if not key:
                raise ProviderConfigurationError(f"environment variable {key_env} is not set")
        timeout: float | None = getattr(arguments, "openai_timeout", None)
        try:
            if timeout is None:
                config = OpenAiConfig(url, model, api_key=key)
            else:
                config = OpenAiConfig(url, model, api_key=key, request_timeout=timeout)
        except ValueError as error:
            raise ProviderConfigurationError(str(error)) from None
        return OpenAiProvider(config)


class OpenCodeProviderFactory:
    """OpenCode V2 server acting as a proxy to its configured model."""

    provider_id = OpenCodeProvider.provider_id
    description = "OpenCode V2 server (requires the pyGdbToolkit[opencode] extra)"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        """Declare endpoint, optional agent, model, password and timeout options."""
        group = parser.add_argument_group("OpenCode V2 provider (--llm opencode)")
        group.add_argument(
            "--opencode-url",
            metavar="URL",
            help="OpenCode V2 server URL (default: $PYGDB_OPENCODE_URL)",
        )
        group.add_argument("--opencode-agent", metavar="AGENT", help="Agent configured on OpenCode")
        group.add_argument(
            "--opencode-model",
            metavar="PROVIDER/MODEL[#VARIANT]",
            help="Model configured on OpenCode (default: the server's selected model)",
        )
        credentials = group.add_mutually_exclusive_group()
        credentials.add_argument(
            "--opencode-password-env",
            metavar="NAME",
            help="Environment variable holding the OpenCode server password",
        )
        credentials.add_argument(
            "--opencode-password-file",
            type=Path,
            metavar="FILE",
            help="UTF-8 file containing the OpenCode server password",
        )
        group.add_argument(
            "--opencode-timeout",
            type=float,
            metavar="SECONDS",
            help="Timeout per request (default: 120)",
        )

    def create(self, arguments: argparse.Namespace, environ: Mapping[str, str]) -> LlmProvider:
        """Resolve the server URL and optional credential without contacting it."""
        url: str | None = getattr(arguments, "opencode_url", None)
        if url is None and environ.get("PYGDB_OPENCODE_URL", "").strip():
            url = environ["PYGDB_OPENCODE_URL"].strip()
        if url is None:
            raise ProviderConfigurationError(
                "OpenCode requires --opencode-url or PYGDB_OPENCODE_URL"
            )
        password: str | None = None
        password_env: str | None = getattr(arguments, "opencode_password_env", None)
        password_file: Path | None = getattr(arguments, "opencode_password_file", None)
        if password_env is not None and password_file is not None:
            raise ProviderConfigurationError(
                "--opencode-password-env and --opencode-password-file are mutually exclusive"
            )
        if password_env is not None:
            if not _ENVIRONMENT_NAME.fullmatch(password_env):
                raise ProviderConfigurationError("--opencode-password-env must name a variable")
            password = environ.get(password_env)
            if password is None or not password.strip():
                raise ProviderConfigurationError(f"environment variable {password_env} is not set")
        elif password_file is not None:
            password = _read_opencode_password(password_file)
        timeout: float | None = getattr(arguments, "opencode_timeout", None)
        try:
            config = OpenCodeConfig(
                url,
                agent=getattr(arguments, "opencode_agent", None),
                model=getattr(arguments, "opencode_model", None),
                password=password,
                request_timeout=120.0 if timeout is None else timeout,
            )
        except ValueError as error:
            raise ProviderConfigurationError(str(error)) from None
        return OpenCodeProvider(config)


def default_registry() -> ProviderRegistry:
    """Return the built-in providers; applications may register more."""
    return ProviderRegistry(
        (CopilotProviderFactory(), OpenAiProviderFactory(), OpenCodeProviderFactory())
    )
