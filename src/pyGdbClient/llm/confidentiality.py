# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Masking of recognizable secrets before data is disclosed to an LLM provider."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
import json
import re

from .contracts import (
    ContextFact,
    JsonValue,
    MemoryMapContext,
    MemoryRegion,
    TargetContext,
    ToolResult,
)

MAX_PATTERN_CHARS = 512

TEST_DATA_WARNING = (
    "TEST DATA ANALYSIS: automatic masking is disabled; target snapshots, prompts and "
    "tool results are disclosed unchanged. Use test cryptographic material only with "
    "external providers. Production data requires a local model on trusted equipment, "
    "not an external provider."
)


@dataclass(frozen=True)
class RedactionRule:
    """A named expression whose every match is replaced before disclosure."""

    name: str
    pattern: re.Pattern[str]

    def __post_init__(self) -> None:
        """Reject rules that are anonymous or would match empty text."""
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", self.name):
            raise ValueError(f"invalid redaction rule name: {self.name!r}")
        if self.pattern.fullmatch(""):
            raise ValueError(f"redaction rule {self.name} matches empty text")


def _rule(name: str, pattern: str, flags: int = 0) -> RedactionRule:
    return RedactionRule(name, re.compile(pattern, flags))


# Bounded quantifiers keep matching linear on the 64 KiB results the gateway allows.
DEFAULT_RULES = (
    _rule(
        "private-key",
        r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----(?:(?!-----END).){0,16384}"
        r"(?:-----END [A-Z0-9 ]{0,40}PRIVATE KEY-----)?",
        re.DOTALL,
    ),
    _rule("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})"),
    _rule("aws-access-key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    _rule("bearer-token", r"(?i)\bbearer\s{1,8}[A-Za-z0-9._~+/-]{8,4096}=*"),
    _rule(
        "credential-assignment",
        r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
        r"\s{0,8}[:=]\s{0,8}[\"']?[^\s\"'\\,;]{1,1024}",
    ),
)


class Redactor:
    """Replace rule matches with ``[REDACTED:<rule>]`` and count the replacements.

    Masking is best effort: it recognizes textual secrets, not key material held
    in raw memory or register values, which can only be protected by not
    reading it.
    """

    def __init__(self, rules: Iterable[RedactionRule] = DEFAULT_RULES) -> None:
        """Use the given rules, the defaults when omitted."""
        self._rules = tuple(rules)
        names = [rule.name for rule in self._rules]
        if len(set(names)) != len(names):
            raise ValueError("redaction rule names must be unique")

    @property
    def rules(self) -> tuple[RedactionRule, ...]:
        """Return the active rules."""
        return self._rules

    @classmethod
    def with_patterns(cls, patterns: Iterable[str], *, defaults: bool = True) -> Redactor:
        """Add auditor expressions, named ``custom-N``, to the optional defaults."""
        rules = list(DEFAULT_RULES if defaults else ())
        for index, pattern in enumerate(patterns, start=1):
            if not pattern or len(pattern) > MAX_PATTERN_CHARS:
                raise ValueError(f"redaction pattern must have 1 to {MAX_PATTERN_CHARS} characters")
            try:
                compiled = re.compile(pattern)
            except re.error as error:
                raise ValueError(f"invalid redaction pattern {pattern!r}: {error}") from None
            rules.append(RedactionRule(f"custom-{index}", compiled))
        return cls(rules)

    def text(self, text: str) -> tuple[str, int]:
        """Return masked text and the number of replaced matches."""
        total = 0
        for rule in self._rules:
            text, count = rule.pattern.subn(f"[REDACTED:{rule.name}]", text)
            total += count
        return text, total

    def result(self, result: ToolResult) -> tuple[ToolResult, int]:
        """Mask JSON string values of a result, preserving its JSON structure."""
        try:
            document = json.loads(result.content)
        except ValueError:
            content, count = self.text(result.content)
        else:
            masked, count = self._json(document)
            content = json.dumps(masked, ensure_ascii=True) if count else result.content
        return replace(result, content=content), count

    def context(self, context: TargetContext) -> tuple[TargetContext, int]:
        """Mask every descriptive string of a snapshot, keeping its identity and addresses."""
        counter = [0]

        def mask(value: str) -> str:
            masked, count = self.text(value)
            counter[0] += count
            return masked

        def optional(value: str | None) -> str | None:
            return None if value is None else mask(value)

        def fact(item: ContextFact) -> ContextFact:
            return ContextFact(
                optional(item.value), mask(item.source), optional(item.unavailable_reason)
            )

        def region(item: MemoryRegion) -> MemoryRegion:
            return MemoryRegion(
                item.start,
                item.end,
                mask(item.kind),
                mask(item.source),
                mask(item.name),
                mask(item.protection),
            )

        memory_map = context.memory_map
        masked = replace(
            context,
            mcu=fact(context.mcu),
            revision=fact(context.revision),
            architecture=fact(context.architecture),
            debug_transport=fact(context.debug_transport),
            ocd=fact(context.ocd),
            core=fact(context.core),
            svd_file=fact(context.svd_file),
            memory_map=MemoryMapContext(
                tuple(region(item) for item in memory_map.regions),
                mask(memory_map.source),
                optional(memory_map.unavailable_reason),
            ),
        )
        return masked, counter[0]

    def _json(self, value: JsonValue) -> tuple[JsonValue, int]:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            items = [self._json(item) for item in value]
            return [item for item, _ in items], sum(count for _, count in items)
        if isinstance(value, dict):
            entries = {key: self._json(item) for key, item in value.items()}
            return (
                {key: item for key, (item, _) in entries.items()},
                sum(count for _, count in entries.values()),
            )
        return value, 0
