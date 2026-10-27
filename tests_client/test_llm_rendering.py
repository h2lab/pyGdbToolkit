# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Agent events are rendered literally, bounded, and without terminal control sequences."""

import pytest
from rich.text import Text

from pyGdbClient.llm.contracts import (
    ActionEffect,
    ActionPlan,
    ContextFact,
    MemoryMapContext,
    MemoryRegion,
    PlannedAction,
    TargetContext,
    ToolCall,
    ToolResult,
)
from pyGdbClient.llm.orchestrator import (
    AgentNotice,
    AgentPlan,
    AgentRunFinished,
    AgentText,
    AgentToolResults,
    RunOutcome,
    RunReason,
)
from pyGdbClient.llm.provider import AuthenticationState, ProviderStatus
from pyGdbClient.llm.rendering import (
    MAX_BUFFERED_TEXT_CHARS,
    MAX_DISPLAYED_REGIONS,
    AgentTranscript,
    describe_context,
    describe_status,
    printable,
)


def _plain(lines: list[Text]) -> list[str]:
    return [line.plain for line in lines]


def _context(regions: int = 1) -> TargetContext:
    return TargetContext(
        "ctx-1",
        ContextFact("STM32U585", "lscpu"),
        ContextFact(None, "lscpu", "revision register unreadable"),
        ContextFact("armv8-m.main", "gdb"),
        ContextFact("swd", "ocd"),
        ContextFact("openocd", "server"),
        ContextFact("0", "gdb"),
        MemoryMapContext(
            tuple(
                MemoryRegion(
                    0x08000000 + index * 0x1000,
                    0x08001000 + index * 0x1000,
                    "flash",
                    "gdb",
                    "bank",
                    "ro",
                )
                for index in range(regions)
            ),
            "gdb",
        ),
        ContextFact("/srv/STM32U585.svd", "svd"),
    )


def test_printable_neutralizes_escape_and_bidirectional_characters() -> None:
    assert printable("a\x1b[2Jb\u202ec\td") == "a\ufffd[2Jb\ufffdc\td"


def test_streamed_text_is_joined_into_complete_lines() -> None:
    transcript = AgentTranscript()
    assert transcript.feed(AgentText("Hel")) == []
    assert _plain(transcript.feed(AgentText("lo\nwor"))) == ["Hello"]
    assert _plain(transcript.feed(AgentText("ld\n\nnext"))) == ["world", ""]
    assert _plain(transcript.flush()) == ["next"]
    assert transcript.flush() == []


def test_unterminated_text_is_bounded() -> None:
    transcript = AgentTranscript()
    lines = transcript.feed(AgentText("x" * MAX_BUFFERED_TEXT_CHARS))
    assert _plain(lines) == ["x" * MAX_BUFFERED_TEXT_CHARS]
    assert transcript.flush() == []


def test_non_text_event_flushes_pending_text_first() -> None:
    transcript = AgentTranscript()
    transcript.feed(AgentText("partial"))
    lines = _plain(transcript.feed(AgentNotice("context refreshed")))
    assert lines == ["partial", "Agent: context refreshed"]


def test_plan_shows_tools_arguments_effects_and_purposes_literally() -> None:
    plan = ActionPlan(
        "plan-1",
        "ctx-1",
        "[bold]Inspect[/] GPIO",
        (
            PlannedAction(
                ToolCall("r1", "memory_read", {"length": 4, "address": 0x48000000}),
                "Read MODER\x1b]0;x\x07",
                (ActionEffect.READ_MEMORY,),
            ),
        ),
    )
    lines = _plain(AgentTranscript().feed(AgentPlan(plan)))
    assert lines == [
        "Agent plan plan-1 · 1 step(s) · context ctx-1",
        "  Rationale: [bold]Inspect[/] GPIO",
        '  1. memory_read {"address": 1207959552, "length": 4} [read_memory]',
        "     Read MODER\ufffd]0;x\ufffd",
    ]


def test_results_are_truncated_and_marked() -> None:
    results = (ToolResult("r1", "abcdef\nline"), ToolResult("r2", "denied", is_error=True))
    lines = AgentTranscript(max_result_chars=4).feed(AgentToolResults(results))
    assert _plain(lines) == [
        "  [ok] r1",
        "    abcd… (7 more characters)",
        "  [error] r2",
        "    deni… (2 more characters)",
    ]
    assert lines[2].style == "red"


def test_invalid_result_bound_is_rejected() -> None:
    with pytest.raises(ValueError):
        AgentTranscript(max_result_chars=0)


@pytest.mark.parametrize(
    ("reason", "style"), [("completed", "bold cyan"), ("cancelled", "bold red")]
)
def test_run_completion_summarizes_usage(reason: RunReason, style: str) -> None:
    outcome = RunOutcome(reason, "done", 2, 3, 10)
    (line,) = AgentTranscript().feed(AgentRunFinished(outcome))
    assert line.plain == f"Agent request {reason}: done · 2 round(s) · 3 tool call(s)"
    assert line.style == style


def test_error_notice_is_highlighted() -> None:
    (line,) = AgentTranscript().feed(AgentNotice("lost", error=True))
    assert line.style == "bold red"


def test_context_description_lists_every_transmitted_fact() -> None:
    lines = _plain(describe_context("session-1", _context()))
    assert lines == [
        "Agent session session-1 · context ctx-1",
        "  MCU: STM32U585 (lscpu)",
        "  Revision: unavailable: revision register unreadable (lscpu)",
        "  Architecture: armv8-m.main (gdb)",
        "  Debug transport: swd (ocd)",
        "  OCD: openocd (server)",
        "  Core: 0 (gdb)",
        "  SVD: /srv/STM32U585.svd (svd)",
        "  Memory map: 1 region(s) (gdb)",
        "    0x08000000-0x08000FFF flash ro bank",
    ]


def test_context_description_bounds_regions_and_reports_missing_maps() -> None:
    lines = _plain(describe_context("s", _context(MAX_DISPLAYED_REGIONS + 2)))
    assert lines[-1] == "    … 2 more region(s)"
    assert len(lines) == 9 + MAX_DISPLAYED_REGIONS + 1
    fact = ContextFact("x", "y")
    missing = TargetContext(
        "ctx-2", fact, fact, fact, fact, fact, fact, MemoryMapContext((), "gdb", "no map"), fact
    )
    assert describe_context("s", missing)[-1].plain == "  Memory map: unavailable: no map (gdb)"


def test_status_reports_readiness_and_guidance() -> None:
    ready = describe_status(
        "scripted", ProviderStatus(True, AuthenticationState.NOT_REQUIRED, "Local")
    )
    assert _plain(ready) == [
        "Agent provider scripted: ready · authentication not_required",
        "  Local",
    ]
    required = describe_status(
        "github-copilot",
        ProviderStatus(True, AuthenticationState.REQUIRED, "Run 'copilot login'."),
    )
    assert required[0].plain.endswith("not ready · authentication required")
    assert required[0].style == "bold red"
