# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Provider selection and launcher options, without network, runtime or target."""

import argparse
import asyncio
import base64
from collections.abc import Mapping
import os
from pathlib import Path
import sys

import httpx
import pytest

from pyGdbClient.cli import _parser, agent_settings
from pyGdbClient.llm.copilot import CopilotProvider
from pyGdbClient.llm.provider import LlmProvider
from pyGdbClient.llm.opencode import OpenCodeProvider
from pyGdbClient.llm.openai_compat import OpenAiProvider
from pyGdbClient.llm.registry import (
    CopilotProviderFactory,
    OpenAiProviderFactory,
    OpenCodeProviderFactory,
    ProviderConfigurationError,
    ProviderRegistry,
    default_registry,
)
from pyGdbClient.llm.scripted import ScriptedProvider


class ScriptedFactory:
    provider_id = "scripted"
    description = "Offline replay"

    def __init__(self, provider_id: str = "scripted") -> None:
        self.provider_id = provider_id
        self.created: list[argparse.Namespace] = []

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument_group("Scripted").add_argument("--scripted-flag", action="store_true")

    def create(self, arguments: argparse.Namespace, environ: Mapping[str, str]) -> LlmProvider:
        self.created.append(arguments)
        return ScriptedProvider(())


def _parse(
    registry: ProviderRegistry, *argv: str
) -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = _parser(registry)
    return parser, parser.parse_args(list(argv))


def test_default_registry_offers_copilot_openai_and_opencode() -> None:
    registry = default_registry()
    assert registry.ids == ("github-copilot", "openai", "opencode")
    assert isinstance(registry.get("github-copilot"), CopilotProviderFactory)
    assert isinstance(registry.get("openai"), OpenAiProviderFactory)
    assert isinstance(registry.get("opencode"), OpenCodeProviderFactory)


def test_opencode_factory_uses_server_options_and_environment_without_network() -> None:
    registry = default_registry()
    parser, arguments = _parse(
        registry,
        "--llm",
        "opencode",
        "--opencode-agent",
        "audit",
        "--opencode-model",
        "ollama/llama3.2:latest",
        "--opencode-password-env",
        "TOKEN",
        "--opencode-timeout",
        "45",
    )
    settings = agent_settings(
        parser,
        arguments,
        registry,
        {
            "PYGDB_OPENCODE_URL": "http://server:4096",
            "TOKEN": "secret-password",
        },
    )
    assert settings is not None and isinstance(settings.provider, OpenCodeProvider)
    config = settings.provider._config
    assert config.base_url == "http://server:4096"
    assert config.agent == "audit" and config.model == "ollama/llama3.2:latest"
    assert config.password == "secret-password" and config.request_timeout == 45
    assert "secret-password" not in repr(config)


@pytest.mark.parametrize("line_ending", [b"", b"\n", b"\r\n"])
def test_opencode_password_file_authenticates_without_disclosing_password(
    tmp_path: Path, line_ending: bytes
) -> None:
    password_file = tmp_path / "password"
    password_file.write_bytes(b" password with spaces " + line_ending)
    registry = default_registry()
    parser, arguments = _parse(
        registry,
        "--llm",
        "opencode",
        "--opencode-url",
        "http://server",
        "--opencode-password-file",
        str(password_file),
    )
    settings = agent_settings(parser, arguments, registry, {})
    assert settings is not None and isinstance(settings.provider, OpenCodeProvider)
    provider = settings.provider
    assert provider._config.password == " password with spaces "
    assert "password with spaces" not in repr(provider._config)

    def handle(request: httpx.Request) -> httpx.Response:
        token = base64.b64encode(b"opencode: password with spaces ").decode("ascii")
        assert request.headers["authorization"] == f"Basic {token}"
        return httpx.Response(200, json={"version": "2.0.0"})

    provider._transport = httpx.MockTransport(handle)

    async def exercise() -> None:
        status = await provider.status()
        assert status.available
        assert status.authentication.value == "authenticated"
        assert "password with spaces" not in status.message
        await provider.close()

    asyncio.run(exercise())


def test_opencode_password_file_expands_home_and_resolves_relative_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "password").write_text("secret\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    registry = default_registry()
    for path in ("~/password", "password"):
        parser, arguments = _parse(
            registry,
            "--llm",
            "opencode",
            "--opencode-url",
            "http://server",
            "--opencode-password-file",
            path,
        )
        settings = agent_settings(parser, arguments, registry, {})
        assert settings is not None and isinstance(settings.provider, OpenCodeProvider)
        assert settings.provider._config.password == "secret"


def test_opencode_password_file_accepts_maximum_size_and_symlinks(tmp_path: Path) -> None:
    password_file = tmp_path / "password"
    password_file.write_bytes(b"x" * (16 * 1024))
    link = tmp_path / "link"
    link.symlink_to(password_file)
    registry = default_registry()
    parser, arguments = _parse(
        registry,
        "--llm",
        "opencode",
        "--opencode-url",
        "http://server",
        "--opencode-password-file",
        str(link),
    )
    settings = agent_settings(parser, arguments, registry, {})
    assert settings is not None and isinstance(settings.provider, OpenCodeProvider)
    assert settings.provider._config.password == "x" * (16 * 1024)


def test_password_file_is_not_read_for_another_provider(tmp_path: Path) -> None:
    registry = default_registry()
    parser, arguments = _parse(
        registry,
        "--llm",
        "openai",
        "--openai-url",
        "http://server/v1",
        "--openai-model",
        "model",
        "--opencode-password-file",
        str(tmp_path / "does-not-exist"),
    )
    settings = agent_settings(parser, arguments, registry, {})
    assert settings is not None and isinstance(settings.provider, OpenAiProvider)


@pytest.mark.parametrize(
    "content,reason",
    [
        (b"", "nonempty"),
        (b"  \n", "nonempty"),
        (b"secret\nsecond-line", "one nonempty line"),
        (b"secret\0hidden", "NUL"),
        (b"\xff", "UTF-8"),
        (b"x" * (16 * 1024 + 1), "16384 bytes"),
    ],
)
def test_invalid_opencode_password_files_are_reported_without_contents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: bytes, reason: str
) -> None:
    password_file = tmp_path / "password"
    password_file.write_bytes(content)
    registry = default_registry()
    parser, arguments = _parse(
        registry,
        "--llm",
        "opencode",
        "--opencode-url",
        "http://server",
        "--opencode-password-file",
        str(password_file),
    )
    with pytest.raises(SystemExit):
        agent_settings(parser, arguments, registry, {})
    error = capsys.readouterr().err
    assert reason in error
    assert "secret" not in error


@pytest.mark.parametrize("kind", ["missing", "directory", "fifo"])
def test_opencode_password_file_access_failures_are_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    path = tmp_path / "credential"
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    registry = default_registry()
    parser, arguments = _parse(
        registry,
        "--llm",
        "opencode",
        "--opencode-url",
        "http://server",
        "--opencode-password-file",
        str(path),
    )
    with pytest.raises(SystemExit):
        agent_settings(parser, arguments, registry, {})
    assert "--opencode-password-file" in capsys.readouterr().err


def test_opencode_password_sources_are_mutually_exclusive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        _parse(
            default_registry(),
            "--llm",
            "opencode",
            "--opencode-password-file",
            "file",
            "--opencode-password-env",
            "NAME",
        )
    assert "not allowed with argument" in capsys.readouterr().err
    with pytest.raises(ProviderConfigurationError, match="mutually exclusive"):
        OpenCodeProviderFactory().create(
            argparse.Namespace(
                opencode_url="http://server",
                opencode_password_file=Path("file"),
                opencode_password_env="NAME",
            ),
            {},
        )


@pytest.mark.parametrize(
    "args",
    [
        ("--llm", "opencode"),
        ("--llm", "opencode", "--opencode-url", "ftp://server"),
        ("--llm", "opencode", "--opencode-url", "http://server", "--opencode-model", "llama"),
        ("--llm", "opencode", "--opencode-url", "http://server", "--opencode-timeout", "0"),
        ("--llm", "opencode", "--opencode-url", "http://server", "--opencode-password-env", "A-B"),
        (
            "--llm",
            "opencode",
            "--opencode-url",
            "http://server",
            "--opencode-password-env",
            "ABSENT",
        ),
    ],
)
def test_opencode_invalid_options_are_reported(args: tuple[str, ...]) -> None:
    registry = default_registry()
    parser, arguments = _parse(registry, *args)
    with pytest.raises(SystemExit):
        agent_settings(parser, arguments, registry, {})


def test_registry_rejects_duplicates_invalid_and_unknown_ids() -> None:
    registry = ProviderRegistry((ScriptedFactory(),))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(ScriptedFactory())
    with pytest.raises(ValueError, match="invalid provider identifier"):
        registry.register(ScriptedFactory("Bad Name"))
    with pytest.raises(ProviderConfigurationError, match="unknown LLM provider"):
        registry.get("missing")


def test_registry_rejects_a_factory_building_another_provider() -> None:
    registry = ProviderRegistry((ScriptedFactory("other"),))
    with pytest.raises(ProviderConfigurationError, match="built provider scripted"):
        registry.create("other", argparse.Namespace(), {})


def test_additional_providers_extend_the_launcher() -> None:
    factory = ScriptedFactory()
    registry = ProviderRegistry((CopilotProviderFactory(), factory))
    parser, arguments = _parse(registry, "--llm", "scripted", "--scripted-flag")
    settings = agent_settings(parser, arguments, registry, {})
    assert settings is not None
    assert isinstance(settings.provider, ScriptedProvider)
    assert factory.created[0].scripted_flag is True
    assert settings.store_path is None
    assert settings.test_data is False
    assert settings.context_limit_chars == 120_000
    assert settings.redactor.text("SN-1234") == ("SN-1234", 0)


def test_confidentiality_and_context_limit_options() -> None:
    registry = ProviderRegistry((ScriptedFactory(),))
    parser, arguments = _parse(
        registry,
        "--llm",
        "scripted",
        "--llm-redact",
        "SN-[0-9]+",
        "--llm-redact",
        "board-[a-z]+",
        "--llm-context-limit",
        "50000",
    )
    settings = agent_settings(parser, arguments, registry, {})
    assert settings is not None
    assert settings.context_limit_chars == 50000
    assert settings.redactor.text("SN-1234 board-x password=y") == (
        "[REDACTED:custom-1] [REDACTED:custom-2] [REDACTED:credential-assignment]",
        3,
    )


def test_agent_is_disabled_without_llm() -> None:
    registry = default_registry()
    parser, arguments = _parse(registry, "ws://server:1")
    assert agent_settings(parser, arguments, registry, {}) is None
    assert arguments.url == "ws://server:1"


def test_test_data_mode_is_explicit() -> None:
    registry = ProviderRegistry((ScriptedFactory(),))
    parser, arguments = _parse(registry, "--llm", "scripted", "--llm-test-data")
    settings = agent_settings(parser, arguments, registry, {})
    assert settings is not None and settings.test_data is True
    assert "local model on trusted equipment" in " ".join(parser.format_help().split())


@pytest.mark.parametrize(
    "argv",
    [
        ("--llm-store", "agent.sqlite3"),
        ("--llm-test-data",),
        ("--llm", "scripted", "--llm-test-data", "--llm-redact", "board"),
        ("--llm-redact", "SN-[0-9]+"),
        ("--llm-context-limit", "50000"),
        ("--llm", "scripted", "--llm-redact", "("),
        ("--llm", "scripted", "--llm-redact", "a*"),
        ("--llm", "scripted", "--llm-context-limit", "100"),
        ("--llm", "github-copilot"),
        ("--llm", "github-copilot", "--copilot-runtime", "/bin/x", "--copilot-token-env", "A-B"),
        ("--llm", "github-copilot", "--copilot-runtime", "/bin/x", "--copilot-token-env", "UNSET"),
        ("--llm", "github-copilot", "--copilot-runtime", "/bin/x", "--copilot-model", " "),
        ("--llm", "openai"),
        ("--llm", "openai", "--openai-url", "http://host.lan:11434/v1"),
        ("--llm", "openai", "--openai-url", "ftp://host/v1", "--openai-model", "m"),
        ("--llm", "openai", "--openai-url", "http://h/v1", "--openai-model", " "),
        ("--llm", "openai", "--openai-url", "http://h/v1", "--openai-model", "m",
         "--openai-key-env", "A-B"),
        ("--llm", "openai", "--openai-url", "http://h/v1", "--openai-model", "m",
         "--openai-key-env", "UNSET"),
        ("--llm", "openai", "--openai-url", "http://h/v1", "--openai-model", "m",
         "--openai-timeout", "0"),
    ],
)
def test_invalid_agent_options_exit_through_the_parser(
    argv: tuple[str, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    registry = ProviderRegistry(
        (CopilotProviderFactory(), OpenAiProviderFactory(), ScriptedFactory())
    )
    parser, arguments = _parse(registry, *argv)
    with pytest.raises(SystemExit):
        agent_settings(parser, arguments, registry, {"EMPTY": " "})
    assert "error:" in capsys.readouterr().err


def test_unknown_provider_is_rejected_by_choices(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _parse(default_registry(), "--llm", "other")
    assert "invalid choice" in capsys.readouterr().err


def test_copilot_uses_cli_login_by_default_and_an_absolute_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    registry = default_registry()
    parser, arguments = _parse(
        registry,
        "--llm",
        "github-copilot",
        "--copilot-runtime",
        "bin/copilot",
        "--copilot-model",
        "gpt-test",
        "--llm-store",
        "~/agent.sqlite3",
    )
    settings = agent_settings(parser, arguments, registry, {})
    assert settings is not None
    provider = settings.provider
    assert isinstance(provider, CopilotProvider)
    config = provider._config
    assert config.runtime_path == tmp_path / "bin" / "copilot"
    assert config.model == "gpt-test"
    assert config.github_token is None
    assert settings.store_path == Path("~/agent.sqlite3").expanduser()
    assert settings.copilot_allow_project_sources is False


def test_copilot_project_sources_require_explicit_cli_consent() -> None:
    registry = default_registry()
    parser, arguments = _parse(
        registry, "--llm", "github-copilot", "--copilot-allow-project-sources"
    )
    settings = agent_settings(
        parser, arguments, registry, {"PYGDB_COPILOT_RUNTIME": sys.executable}
    )
    assert settings is not None
    assert settings.copilot_allow_project_sources is True
    assert "disclose source contents to GitHub Copilot" in " ".join(parser.format_help().split())


@pytest.mark.parametrize("provider", [None, "opencode", "openai"])
def test_copilot_project_source_consent_cannot_be_used_with_another_provider(
    provider: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ("--copilot-allow-project-sources",)
    if provider is not None:
        argv += ("--llm", provider)
    registry = default_registry()
    parser, arguments = _parse(registry, *argv)
    with pytest.raises(SystemExit):
        agent_settings(parser, arguments, registry, {})
    assert (
        "--copilot-allow-project-sources requires --llm github-copilot" in capsys.readouterr().err
    )


def test_copilot_reads_runtime_and_token_from_the_environment_only() -> None:
    registry = default_registry()
    parser, arguments = _parse(registry, "--llm", "github-copilot", "--copilot-token-env", "TOK")
    environ = {"PYGDB_COPILOT_RUNTIME": sys.executable, "TOK": " secret-token "}
    settings = agent_settings(parser, arguments, registry, environ)
    assert settings is not None
    assert isinstance(settings.provider, CopilotProvider)
    config = settings.provider._config
    assert config.runtime_path == Path(sys.executable)
    assert config.github_token == "secret-token"
    assert "secret-token" not in repr(config)
    assert "secret-token" not in _parser(registry).format_help()


def test_openai_reads_endpoint_and_key_from_options_and_environment() -> None:
    registry = default_registry()
    parser, arguments = _parse(
        registry,
        "--llm",
        "openai",
        "--openai-url",
        "http://ollama.lan:11434/v1",
        "--openai-model",
        "qwen3",
        "--openai-key-env",
        "KEY",
        "--openai-timeout",
        "30",
    )
    settings = agent_settings(parser, arguments, registry, {"KEY": " secret-key "})
    assert settings is not None
    provider = settings.provider
    assert isinstance(provider, OpenAiProvider)
    config = provider._config
    assert config.base_url == "http://ollama.lan:11434/v1"
    assert config.model == "qwen3"
    assert config.api_key == "secret-key"
    assert config.request_timeout == 30
    assert "secret-key" not in repr(config)
    assert "secret-key" not in _parser(registry).format_help()


def test_openai_defaults_come_from_the_environment_without_a_key() -> None:
    registry = default_registry()
    parser, arguments = _parse(registry, "--llm", "openai")
    environ = {"PYGDB_OPENAI_URL": " http://llama.lan:8080/v1 ", "PYGDB_OPENAI_MODEL": " m "}
    settings = agent_settings(parser, arguments, registry, environ)
    assert settings is not None
    assert isinstance(settings.provider, OpenAiProvider)
    config = settings.provider._config
    assert config.base_url == "http://llama.lan:8080/v1"
    assert config.model == "m"
    assert config.api_key is None
    assert config.request_timeout == 120.0
