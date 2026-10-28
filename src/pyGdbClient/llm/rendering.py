# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Literal console rendering of agent events, independent of the dashboard toolkit."""

from __future__ import annotations

import json

from rich.text import Text

from .contracts import ContextFact, TargetContext
from .controller import ContextUsage, OpenedSession
from .orchestrator import (
    AgentEvent,
    AgentNotice,
    AgentPlan,
    AgentRunFinished,
    AgentText,
    AgentToolResults,
)
from .provider import AuthenticationState, ProviderStatus
from .store import StoredSession

MAX_RESULT_DISPLAY_CHARS = 2000
MAX_DISPLAYED_REGIONS = 16
MAX_BUFFERED_TEXT_CHARS = 4000

_TEXT_STYLE = "green"
_ERROR_STYLE = "bold red"
_INFO_STYLE = "bold cyan"


def printable(text: str) -> str:
    """Replace control and format characters, so model output cannot drive the terminal."""
    return "".join(
        character if character == "\t" or character.isprintable() else "\ufffd"
        for character in text
    )


def _lines(text: str, style: str, indent: str = "") -> list[Text]:
    return [Text(f"{indent}{printable(line)}", style=style) for line in text.split("\n")]


def _fact(fact: ContextFact) -> str:
    if fact.value is not None:
        return f"{fact.value} ({fact.source})"
    return f"unavailable: {fact.unavailable_reason} ({fact.source})"


def describe_context(session_id: str, context: TargetContext) -> list[Text]:
    """Show the auditor exactly which target facts were transmitted to the provider."""
    memory_map = context.memory_map
    entries = [
        f"  MCU: {_fact(context.mcu)}",
        f"  Revision: {_fact(context.revision)}",
        f"  Architecture: {_fact(context.architecture)}",
        f"  Debug transport: {_fact(context.debug_transport)}",
        f"  OCD: {_fact(context.ocd)}",
        f"  Core: {_fact(context.core)}",
        f"  SVD: {_fact(context.svd_file)}",
    ]
    if memory_map.unavailable_reason is not None:
        entries.append(
            f"  Memory map: unavailable: {memory_map.unavailable_reason} ({memory_map.source})"
        )
    else:
        entries.append(f"  Memory map: {len(memory_map.regions)} region(s) ({memory_map.source})")
        for region in memory_map.regions[:MAX_DISPLAYED_REGIONS]:
            name = f" {region.name}" if region.name else ""
            entries.append(
                f"    0x{region.start:08X}-0x{region.end - 1:08X} {region.kind}"
                f" {region.protection}{name}"
            )
        hidden = len(memory_map.regions) - MAX_DISPLAYED_REGIONS
        if hidden > 0:
            entries.append(f"    … {hidden} more region(s)")
    heading = f"Agent session {session_id} · context {context.context_id}"
    return [
        Text(printable(heading), style=_INFO_STYLE),
        *(Text(printable(entry)) for entry in entries),
    ]


def describe_session(session: OpenedSession) -> list[Text]:
    """Describe a newly opened conversation, its disclosure and its continuity."""
    lines = describe_context(session.session_id, session.context)
    if session.test_data:
        lines.append(Text("  Test data analysis: automatic masking disabled", style="yellow"))
    if session.redactions:
        lines.append(
            Text(
                f"  Redacted before disclosure: {session.redactions} value(s)",
                style="yellow",
            )
        )
    if session.continues is not None:
        detail = (
            f"{session.recollection_chars} characters of recollection"
            if session.recollection_chars
            else "no recorded history to recollect"
        )
        lines.append(Text(printable(f"  Continues {session.continues}: {detail}"), style="yellow"))
    return lines


def describe_usage(usage: ContextUsage | None) -> Text:
    """Summarize the open conversation's size against its continuation threshold."""
    if usage is None:
        return Text("Agent session: none open")
    percent = min(100, usage.used * 100 // usage.limit)
    return Text(f"Agent session context: {usage.used} / {usage.limit} characters ({percent}%)")


def describe_sessions(sessions: tuple[StoredSession, ...]) -> list[Text]:
    """List journaled conversations that ``/resume`` can continue."""
    if not sessions:
        return [Text("No journaled agent session")]
    lines = [Text("Journaled agent sessions (oldest first)", style=_INFO_STYLE)]
    for session in sessions:
        title = f" · {session.title}" if session.title else ""
        lines.append(
            Text(
                printable(
                    f"  {session.session_id}  {session.created_at}  "
                    f"{session.provider_id}{title}"
                )
            )
        )
    return lines


def describe_status(provider_id: str, status: ProviderStatus) -> list[Text]:
    """Render provider readiness and authentication guidance without credentials."""
    ready = status.available and status.authentication in {
        AuthenticationState.AUTHENTICATED,
        AuthenticationState.NOT_REQUIRED,
    }
    heading = Text(
        f"Agent provider {printable(provider_id)}: "
        f"{'ready' if ready else 'not ready'} · authentication {status.authentication.value}",
        style=_INFO_STYLE if ready else _ERROR_STYLE,
    )
    return [heading, *_lines(status.message, "" if ready else "red", "  ")]


class AgentTranscript:
    """Convert orchestrator events to literal lines, joining streamed text deltas."""

    def __init__(self, max_result_chars: int = MAX_RESULT_DISPLAY_CHARS) -> None:
        """Bound the displayed size of each tool observation."""
        if max_result_chars < 1:
            raise ValueError("max_result_chars must be positive")
        self._max_result_chars = max_result_chars
        self._pending = ""

    def feed(self, event: AgentEvent) -> list[Text]:
        """Return the lines that became complete with this event."""
        if isinstance(event, AgentText):
            self._pending += event.text
            if "\n" not in self._pending and len(self._pending) < MAX_BUFFERED_TEXT_CHARS:
                return []
            complete, _, self._pending = self._pending.rpartition("\n")
            if not complete:
                complete, self._pending = self._pending, ""
            return _lines(complete, _TEXT_STYLE)
        lines = self.flush()
        if isinstance(event, AgentPlan):
            lines.extend(self._plan(event))
        elif isinstance(event, AgentToolResults):
            lines.extend(self._results(event))
        elif isinstance(event, AgentNotice):
            lines.extend(_lines(f"Agent: {event.message}", _ERROR_STYLE if event.error else ""))
        elif isinstance(event, AgentRunFinished):
            outcome = event.outcome
            lines.append(
                Text(
                    printable(
                        f"Agent request {outcome.reason}: {outcome.message} · "
                        f"{outcome.rounds} round(s) · {outcome.tool_calls} tool call(s)"
                    ),
                    style=_INFO_STYLE if outcome.reason == "completed" else _ERROR_STYLE,
                )
            )
        return lines

    def flush(self) -> list[Text]:
        """Return any buffered partial line."""
        pending, self._pending = self._pending, ""
        return _lines(pending, _TEXT_STYLE) if pending else []

    @staticmethod
    def _plan(event: AgentPlan) -> list[Text]:
        plan = event.plan
        lines = [
            Text(
                printable(
                    f"Agent plan {plan.plan_id} · {len(plan.steps)} step(s) · "
                    f"context {plan.target_context_id}"
                ),
                style=_INFO_STYLE,
            ),
            *_lines(f"Rationale: {plan.rationale}", "dim", "  "),
        ]
        for index, step in enumerate(plan.steps, start=1):
            arguments = json.dumps(step.call.arguments, ensure_ascii=True, sort_keys=True)
            effects = ", ".join(effect.value for effect in step.effects)
            lines.append(
                Text(printable(f"  {index}. {step.call.name} {arguments} [{effects}]"), "yellow")
            )
            lines.extend(_lines(step.purpose, "dim", "     "))
        return lines

    def _results(self, event: AgentToolResults) -> list[Text]:
        lines: list[Text] = []
        for result in event.results:
            content = result.content
            hidden = len(content) - self._max_result_chars
            if hidden > 0:
                content = f"{content[: self._max_result_chars]}… ({hidden} more characters)"
            marker = "error" if result.is_error else "ok"
            lines.append(
                Text(
                    printable(f"  [{marker}] {result.call_id}"),
                    style="red" if result.is_error else "cyan",
                )
            )
            lines.extend(_lines(content, "", "    "))
        return lines
