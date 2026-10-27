# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""A stored conversation is recalled within a bounded, clearly untrusted budget."""

import pytest

from pyGdbClient.llm.contracts import JsonObject
from pyGdbClient.llm.recollection import (
    MAX_ENTRY_CHARS,
    MIN_RECOLLECTION_CHARS,
    RECOLLECTION_HEADER,
    build_recollection,
)
from pyGdbClient.llm.store import EntryKind, HistoryEntry


def _entry(
    sequence: int, kind: EntryKind, payload: JsonObject, context: str = "ctx-1"
) -> HistoryEntry:
    return HistoryEntry(sequence, context, kind, payload, "2026-01-01T00:00:00Z")


def test_empty_history_has_no_recollection() -> None:
    assert build_recollection("s-1", [], MIN_RECOLLECTION_CHARS) is None


def test_every_kind_is_described_in_chronological_order() -> None:
    entries = [
        _entry(1, EntryKind.USER, {"text": "read the clock"}),
        _entry(
            2,
            EntryKind.TOOL_CALL,
            {"call_id": "c1", "name": "memory_read", "arguments": {"length": 4}},
        ),
        _entry(3, EntryKind.TOOL_RESULT, {"call_id": "c1", "content": "0x1", "is_error": True}),
        _entry(4, EntryKind.ASSISTANT, {"text": "HSI selected"}, "ctx-2"),
        _entry(5, EntryKind.ERROR, {"reason": "cancelled", "message": "by auditor"}),
    ]
    text = build_recollection("s-1", entries, 2000)
    assert text is not None
    assert text.startswith(f"{RECOLLECTION_HEADER}\nSession: s-1\n")
    assert "Recorded against target context(s): ctx-1, ctx-2" in text
    assert "omitted" not in text
    assert text.splitlines()[-5:] == [
        "Auditor: read the clock",
        'Tool call c1: memory_read {"length": 4}',
        "Tool result c1 (error): 0x1",
        "Agent: HSI selected",
        "Request stopped: cancelled: by auditor",
    ]


def test_budget_keeps_newest_entries_and_counts_omitted_ones() -> None:
    entries = [
        _entry(index, EntryKind.USER, {"text": f"q{index} " + "x" * 200}) for index in range(30)
    ]
    text = build_recollection("s-1", entries, MIN_RECOLLECTION_CHARS)
    assert text is not None
    assert len(text) <= MIN_RECOLLECTION_CHARS
    kept = [line for line in text.splitlines() if line.startswith("Auditor: ")]
    assert kept and kept[-1].startswith("Auditor: q29 ")
    assert f"({30 - len(kept)} earlier entries omitted)" in text


def test_long_entries_are_clipped_and_summaries_capped() -> None:
    long = "y" * (MAX_ENTRY_CHARS + 50)
    text = build_recollection("s-1", [_entry(1, EntryKind.ASSISTANT, {"text": long})], 2000)
    assert text is not None and "… (50 more)" in text
    summary = _entry(1, EntryKind.SUMMARY, {"text": "z" * 5000})
    recalled = build_recollection(
        "s-2", [summary, _entry(2, EntryKind.USER, {"text": "new"})], 4000
    )
    assert recalled is not None
    assert "Earlier recollection:" in recalled and "… (3000 more)" in recalled
    assert recalled.endswith("Auditor: new")


def test_oversized_entry_is_clipped_to_the_remaining_budget() -> None:
    entries = [
        _entry(1, EntryKind.USER, {"text": "older"}),
        _entry(2, EntryKind.SUMMARY, {"text": "z" * 5000}),
        _entry(3, EntryKind.USER, {"text": "latest"}),
    ]
    text = build_recollection("s-1", entries, MIN_RECOLLECTION_CHARS)
    assert text is not None and len(text) <= MIN_RECOLLECTION_CHARS
    assert "(1 earlier entry omitted)" in text
    assert "Earlier recollection:" in text and "more)" in text
    assert text.endswith("Auditor: latest")


def test_entries_are_dropped_once_the_budget_is_nearly_spent() -> None:
    entries = [_entry(index, EntryKind.USER, {"text": "w" * 500}) for index in range(3)]
    text = build_recollection("s-1", entries, MIN_RECOLLECTION_CHARS)
    assert text is not None and len(text) <= MIN_RECOLLECTION_CHARS
    assert text.count("Auditor: ") == 1
    assert "(2 earlier entries omitted)" in text


@pytest.mark.parametrize("limit", [MIN_RECOLLECTION_CHARS - 1, True, 1500.0])
def test_budget_is_validated(limit: object) -> None:
    with pytest.raises(ValueError, match="max_chars"):
        build_recollection("s-1", [], limit)  # type: ignore[arg-type]
