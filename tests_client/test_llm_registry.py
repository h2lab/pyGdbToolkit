# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Provider selection and launcher options, without network, runtime or target."""

import argparse
from collections.abc import Mapping
from pathlib import Path
import sys

import pytest

from pyGdbClient.cli import _parser, agent_settings
from pyGdbClient.llm.copilot import CopilotProvider
from pyGdbClient.llm.provider import LlmProvider
from pyGdbClient.llm.registry import (
    CopilotProviderFactory,
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


def test_default_registry_offers_copilot() -> None:
    registry = default_registry()
    assert registry.ids == ("github-copilot",)
    assert isinstance(registry.get("github-copilot"), CopilotProviderFactory)


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
    ],
)
def test_invalid_agent_options_exit_through_the_parser(
    argv: tuple[str, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    registry = ProviderRegistry((CopilotProviderFactory(), ScriptedFactory()))
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
