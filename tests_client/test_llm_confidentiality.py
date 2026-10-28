# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Recognizable secrets are masked before disclosure, preserving structure."""

import json
import re

import pytest

from pyGdbClient.llm.confidentiality import (
    DEFAULT_RULES,
    MAX_PATTERN_CHARS,
    RedactionRule,
    Redactor,
)
from pyGdbClient.llm.contracts import (
    ContextFact,
    MemoryMapContext,
    MemoryRegion,
    TargetContext,
    ToolResult,
)

_GITHUB = "ghp_" + "a1" * 18
_PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END RSA PRIVATE KEY-----"


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        (f"key {_PEM} tail", "private-key"),
        ("-----BEGIN PRIVATE KEY-----\nMIIBOgIB truncated", "private-key"),
        (f"token {_GITHUB}", "github-token"),
        ("github_pat_" + "B" * 30, "github-token"),
        ("id AKIAABCDEFGHIJKLMNOP end", "aws-access-key"),
        ("Authorization: Bearer abc.def-ghi_jkl", "bearer-token"),
        ("password = hunter22", "credential-assignment"),
        ('api_key: "s3cr3t"', "credential-assignment"),
        ("AUTH-TOKEN=xyz", "credential-assignment"),
    ],
)
def test_default_rules_mask_recognizable_secrets(text: str, rule: str) -> None:
    masked, count = Redactor().text(text)
    assert count >= 1
    assert f"[REDACTED:{rule}]" in masked


def test_harmless_text_is_untouched() -> None:
    text = "PC=0x08000134 reset handler, token bucket of 4 entries"
    assert Redactor().text(text) == (text, 0)


def test_secret_value_never_survives() -> None:
    masked, _ = Redactor().text(f"export GH={_GITHUB}\n{_PEM}")
    assert _GITHUB not in masked
    assert "MIIBOgIBAAJBAK" not in masked


def test_json_results_keep_structure_and_only_change_when_masked() -> None:
    redactor = Redactor()
    plain = ToolResult("c1", '{"value":  1}')
    assert redactor.result(plain) == (plain, 0)
    secret = ToolResult("c2", json.dumps({"lines": ["ok", "pwd=abc"], "n": 2}), True)
    masked, count = redactor.result(secret)
    assert count == 1
    assert masked.call_id == "c2" and masked.is_error
    document = json.loads(masked.content)
    assert document["n"] == 2
    assert document["lines"][0] == "ok"
    assert document["lines"][1].startswith("[REDACTED:")


def test_non_json_results_are_masked_as_text() -> None:
    masked, count = Redactor().result(ToolResult("c1", "raw secret=abc"))
    assert count == 1
    assert masked.content == "raw [REDACTED:credential-assignment]"


def test_context_masks_descriptions_but_keeps_identity_and_addresses() -> None:
    leak = ContextFact("secret=boardkey", "file:pwd=x")
    missing = ContextFact(None, "probe", "auth_token=y")
    plain = ContextFact("stm32", "configuration")
    context = TargetContext(
        "ctx-1",
        leak,
        plain,
        missing,
        plain,
        plain,
        plain,
        MemoryMapContext(
            (MemoryRegion(0x0, 0x1000, "flash", "api_key=k", "boot", "rx"),),
            "secret=s",
        ),
        plain,
    )
    masked, count = Redactor().context(context)
    assert count == 5
    assert masked.context_id == "ctx-1"
    assert masked.revision == plain
    assert masked.mcu.value == "[REDACTED:credential-assignment]"
    region = masked.memory_map.regions[0]
    assert (region.start, region.end, region.kind, region.name) == (0x0, 0x1000, "flash", "boot")
    assert "api_key" not in region.source
    assert Redactor().context(masked)[1] == 0


def test_custom_patterns_extend_or_replace_defaults() -> None:
    redactor = Redactor.with_patterns([r"SN-\d{6}", "board-[a-z]+"])
    assert [rule.name for rule in redactor.rules][-2:] == ["custom-1", "custom-2"]
    assert redactor.text("SN-123456 board-alpha") == (
        "[REDACTED:custom-1] [REDACTED:custom-2]",
        2,
    )
    only = Redactor.with_patterns(["SN-1"], defaults=False)
    assert only.text(f"SN-1 {_GITHUB}") == (f"[REDACTED:custom-1] {_GITHUB}", 1)


@pytest.mark.parametrize(
    ("pattern", "message"),
    [
        ("", "1 to"),
        ("a" * (MAX_PATTERN_CHARS + 1), "1 to"),
        ("(", "invalid redaction pattern"),
        ("a*", "matches empty text"),
    ],
)
def test_invalid_custom_patterns_are_rejected(pattern: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Redactor.with_patterns([pattern])


def test_rule_names_are_validated_and_unique() -> None:
    with pytest.raises(ValueError, match="invalid redaction rule name"):
        RedactionRule("Bad Name", re.compile("x"))
    with pytest.raises(ValueError, match="unique"):
        Redactor((DEFAULT_RULES[0], DEFAULT_RULES[0]))


def test_long_inputs_stay_linear() -> None:
    masked, count = Redactor().text("-----BEGIN PRIVATE KEY-----" + "A" * 60000)
    assert count == 1
    assert len(masked) < 60000
