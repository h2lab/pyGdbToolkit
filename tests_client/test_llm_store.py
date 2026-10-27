# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Conversation persistence works offline, including failures and concurrent writers."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from pyGdbClient.llm.contracts import (
    ContextFact,
    JsonObject,
    MemoryMapContext,
    MemoryRegion,
    TargetContext,
)
from pyGdbClient.llm.store import ContextStore, EntryKind, StoreError


def _context(context_id: str = "ctx-target-1") -> TargetContext:
    return TargetContext(
        context_id=context_id,
        mcu=ContextFact("RP2350", "configuration"),
        revision=ContextFact(None, "configuration", "not declared"),
        architecture=ContextFact("arm", "gdb"),
        debug_transport=ContextFact("SWD", "configuration"),
        ocd=ContextFact("openocd", "configuration"),
        core=ContextFact("rp2350.cm0 (id 0)", "debug-server"),
        memory_map=MemoryMapContext(
            (MemoryRegion(0x20000000, 0x20082000, "ram", "gdb-server", "SRAM", "rw"),),
            "toolkit-discovery",
        ),
        svd_file=ContextFact("/srv/rp2350.svd", "svd"),
    )


def test_session_and_all_history_kinds_survive_reopening(tmp_path: Path) -> None:
    """The journal preserves exact facts, nested JSON, order and UTC timestamps."""
    path = tmp_path / "conversations.sqlite3"
    payloads: list[tuple[EntryKind, JsonObject]] = [
        (EntryKind.USER, {"text": "Inspecter le GPIO"}),
        (EntryKind.ASSISTANT, {"text": "Lecture proposee"}),
        (
            EntryKind.TOOL_CALL,
            {"call_id": "read-1", "name": "registers", "arguments": {"names": ["r0", "r1"]}},
        ),
        (EntryKind.TOOL_RESULT, {"call_id": "read-1", "content": "r0=0", "is_error": False}),
        (EntryKind.SUMMARY, {"text": "GPIO stable", "through_sequence": 4}),
        (EntryKind.ERROR, {"message": "turn cancelled", "partial": True, "extra": None}),
    ]
    with ContextStore(path) as store:
        session = store.create_session("audit-1", "scripted", _context(), "GPIO audit")
        for kind, payload in payloads:
            store.append("audit-1", kind, payload)

    with ContextStore(path) as reopened:
        assert reopened.get_session("audit-1") == session
        assert reopened.list_sessions() == (session,)
        assert reopened.get_context("audit-1") == _context()
        history = reopened.history("audit-1")
        assert [(entry.kind, entry.payload) for entry in history] == payloads
        assert [entry.sequence for entry in history] == list(range(1, 7))
        assert all(entry.context_id == "ctx-target-1" for entry in history)
        assert all(
            datetime.fromisoformat(entry.created_at).utcoffset() == timedelta(0)
            for entry in history
        )
        assert datetime.fromisoformat(session.created_at).tzinfo is not None


def test_context_changes_keep_old_entries_bound_to_old_snapshots(tmp_path: Path) -> None:
    """Switching a core does not rewrite the context of previous observations."""
    initial = _context()
    switched = replace(
        initial, context_id="ctx-target-2", core=ContextFact("rp2350.cm1 (id 1)", "debug-server")
    )
    with ContextStore(tmp_path / "history.db") as store:
        store.create_session("a", "scripted", initial)
        first = store.append("a", EntryKind.USER, {"text": "core 0"})
        store.save_context("a", switched)
        second = store.append("a", EntryKind.USER, {"text": "core 1"})
        old_result = store.append(
            "a", EntryKind.TOOL_RESULT, {"content": "late"}, initial.context_id
        )
        assert [entry.context_id for entry in store.history("a")] == [
            initial.context_id,
            switched.context_id,
            initial.context_id,
        ]
        assert (first.sequence, second.sequence, old_result.sequence) == (1, 2, 3)
        assert store.get_context("a") == switched
        assert store.get_context("a", initial.context_id) == initial
        store.save_context("a", initial)
        store.save_context("a", initial)
        assert store.get_context("a") == initial


def test_snapshot_id_collision_fails_without_modifying_session(tmp_path: Path) -> None:
    """An ID cannot be rebound to different facts, even after reopening."""
    path = tmp_path / "history.db"
    with ContextStore(path) as store:
        store.create_session("a", "scripted", _context())
    with ContextStore(path) as store:
        conflicting = replace(_context(), mcu=ContextFact("Other MCU", "configuration"))
        with pytest.raises(StoreError, match="different facts"):
            store.save_context("a", conflicting)
        assert store.get_context("a") == _context()
        assert store.get_session("a").context_id == _context().context_id


def test_sessions_and_snapshots_are_isolated_and_deletion_cascades(tmp_path: Path) -> None:
    """Deleting one audit leaves no orphan rows and does not touch another audit."""
    path = tmp_path / "history.db"
    with ContextStore(path) as store:
        store.create_session("a", "scripted", _context())
        other = replace(_context(), mcu=ContextFact("Other MCU", "configuration"))
        store.create_session("b", "other-provider", other)
        store.save_context("a", _context("ctx-extra"))
        store.append("a", EntryKind.ASSISTANT, {"text": "a"})
        store.append("b", EntryKind.ASSISTANT, {"text": "b"})
        assert store.get_context("b") == other
        assert store.history("b")[0].sequence == 1
        with pytest.raises(KeyError, match="unknown target context"):
            store.append("b", EntryKind.USER, {"text": "wrong target"}, "ctx-extra")
        store.delete_session("a")
        assert [session.session_id for session in store.list_sessions()] == ["b"]
        assert store.history("b")[0].payload == {"text": "b"}
        with pytest.raises(KeyError, match="unknown session"):
            store.history("a")
        with pytest.raises(KeyError, match="unknown session"):
            store.delete_session("a")
    with closing(sqlite3.connect(path)) as connection, connection:
        for table in ("sessions", "contexts", "entries"):
            assert (
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE session_id = 'a'"
                ).fetchone()[0]
                == 0
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_history_and_session_listing_are_paginated(tmp_path: Path) -> None:
    """The exclusive cursor returns every entry exactly once in bounded pages."""
    with ContextStore(tmp_path / "history.db") as store:
        store.create_session("a", "scripted", _context())
        store.create_session("b", "scripted", _context())
        store.create_session("c", "scripted", _context())
        assert [s.session_id for s in store.list_sessions(2)] == ["a", "b"]
        assert [s.session_id for s in store.list_sessions(2, 2)] == ["c"]
        for number in range(5):
            store.append("a", EntryKind.USER, {"number": number})
        first = store.history("a", limit=2)
        second = store.history("a", after_sequence=first[-1].sequence, limit=2)
        last = store.history("a", after_sequence=second[-1].sequence, limit=2)
        assert [e.sequence for e in (*first, *second, *last)] == [1, 2, 3, 4, 5]
        assert store.history("a", after_sequence=5) == ()


def test_duplicate_session_and_unknown_references_fail_explicitly(tmp_path: Path) -> None:
    """Invalid references cannot create partial sessions, entries or snapshots."""
    with ContextStore(tmp_path / "history.db") as store:
        original = store.create_session("a", "scripted", _context())
        with pytest.raises(sqlite3.IntegrityError):
            store.create_session("a", "other", _context("other"))
        assert store.get_session("a") == original
        with pytest.raises(KeyError):
            store.get_context("a", "other")
        for operation in (
            lambda: store.get_session("missing"),
            lambda: store.get_context("missing"),
            lambda: store.save_context("missing", _context()),
            lambda: store.append("missing", EntryKind.USER, {}),
        ):
            with pytest.raises(KeyError, match="unknown session"):
                operation()
        with pytest.raises(KeyError, match="unknown target context"):
            store.append("a", EntryKind.USER, {}, "missing-context")
        assert store.append("a", EntryKind.USER, {}).sequence == 1


def test_payloads_are_detached_and_sql_characters_are_data(tmp_path: Path) -> None:
    """Mutation and SQL-looking text cannot alter a persisted observation."""
    session_id = "audit'); DROP TABLE sessions; --"
    payload: JsonObject = {"nested": [{"value": "original"}], "text": "'; DELETE FROM entries; --"}
    with ContextStore(tmp_path / "history.db") as store:
        store.create_session(session_id, "scripted", _context(), "L'auditeur")
        entry = store.append(session_id, EntryKind.USER, payload)
        payload["nested"] = "mutated input"
        entry.payload["nested"] = "mutated result"
        loaded = store.history(session_id)[0]
        assert loaded.payload["nested"] == [{"value": "original"}]
        loaded.payload.clear()
        assert store.history(session_id)[0].payload["nested"] == [{"value": "original"}]
        assert store.get_session(session_id).title == "L'auditeur"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"bad": float("nan")},
        {"bad": float("inf")},
        {3: "non-string key"},
        {"bad": object()},
        {"bad": ("not", "JSON")},
        {"bad": {1, 2}},
    ],
)
def test_invalid_json_never_enters_history(tmp_path: Path, payload: Any) -> None:
    """Only finite JSON objects are accepted; failed writes consume no sequence."""
    with ContextStore(tmp_path / "history.db") as store:
        store.create_session("a", "scripted", _context())
        with pytest.raises(ValueError, match="JSON"):
            store.append("a", EntryKind.USER, payload)
        assert store.history("a") == ()
        assert store.append("a", EntryKind.USER, {}).sequence == 1


@pytest.mark.parametrize("limit,offset", [(0, 0), (1001, 0), (True, 0), (1, -1), (1, True)])
def test_pagination_rejects_invalid_values(tmp_path: Path, limit: int, offset: int) -> None:
    """Booleans, unbounded sizes and negative offsets are not valid cursors."""
    with ContextStore(tmp_path / "history.db") as store:
        store.create_session("a", "scripted", _context())
        with pytest.raises(ValueError):
            store.list_sessions(limit, offset)
        with pytest.raises(ValueError):
            store.history("a", offset, limit)


def test_invalid_session_metadata_and_entry_kind_are_rejected(tmp_path: Path) -> None:
    """Missing identifiers and unsupported record kinds are explicit input errors."""
    with ContextStore(tmp_path / "history.db") as store:
        for session_id, provider_id in (("", "scripted"), ("a", " ")):
            with pytest.raises(ValueError, match="non-empty"):
                store.create_session(session_id, provider_id, _context())
        with pytest.raises(ValueError, match="title"):
            store.create_session("a", "scripted", _context(), None)  # type: ignore[arg-type]
        store.create_session("a", "scripted", _context())
        with pytest.raises(ValueError, match="EntryKind"):
            store.append("a", "unknown", {})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "version,application,foreign_table",
    [
        (2, 0x5047444C, False),
        (1, 0, False),
        (0, 42, False),
        (0, 0, True),
    ],
)
def test_incompatible_databases_are_refused_without_changes(
    tmp_path: Path, version: int, application: int, foreign_table: bool
) -> None:
    """Newer, foreign and unversioned nonempty databases are never overwritten."""
    path = tmp_path / "foreign.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(f"PRAGMA user_version = {version}")
        connection.execute(f"PRAGMA application_id = {application}")
        if foreign_table:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.execute("INSERT INTO unrelated VALUES ('preserve me')")
    before = path.read_bytes()
    with pytest.raises(StoreError, match="unsupported"):
        ContextStore(path)
    assert path.read_bytes() == before


def test_failed_write_rolls_back_both_session_and_snapshot(tmp_path: Path) -> None:
    """A snapshot insertion failure rolls back the preceding session insertion."""
    path = tmp_path / "history.db"
    with ContextStore(path):
        pass
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("""
            CREATE TRIGGER reject_context BEFORE INSERT ON contexts
            BEGIN SELECT RAISE(ABORT, 'snapshot write failed'); END
        """)
    with ContextStore(path) as store:
        with pytest.raises(sqlite3.IntegrityError, match="snapshot write failed"):
            store.create_session("a", "scripted", _context())
        assert store.list_sessions() == ()


def test_context_registration_rolls_back_if_selection_fails(tmp_path: Path) -> None:
    """Failure selecting a newly inserted snapshot also rolls back that snapshot."""
    path = tmp_path / "history.db"
    with ContextStore(path) as store:
        store.create_session("a", "scripted", _context())
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("""
            CREATE TRIGGER reject_selection BEFORE UPDATE ON sessions
            BEGIN SELECT RAISE(ABORT, 'context selection failed'); END
        """)
    with ContextStore(path) as store:
        with pytest.raises(sqlite3.IntegrityError, match="context selection failed"):
            store.save_context("a", _context("new"))
        assert store.get_context("a") == _context()
        with pytest.raises(KeyError, match="unknown target context"):
            store.get_context("a", "new")


def test_failed_entry_insertion_does_not_consume_a_sequence(tmp_path: Path) -> None:
    """A failed append is reported and leaves the next successful sequence unchanged."""
    path = tmp_path / "history.db"
    with ContextStore(path) as store:
        store.create_session("a", "scripted", _context())
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("""
            CREATE TRIGGER reject_entry BEFORE INSERT ON entries
            WHEN NEW.kind = 'error'
            BEGIN SELECT RAISE(ABORT, 'entry write failed'); END
        """)
    with ContextStore(path) as store:
        with pytest.raises(sqlite3.IntegrityError, match="entry write failed"):
            store.append("a", EntryKind.ERROR, {"message": "failure"})
        assert store.history("a") == ()
        assert store.append("a", EntryKind.USER, {"text": "next"}).sequence == 1


def test_concurrent_store_instances_serialize_sequences(tmp_path: Path) -> None:
    """Independent connections cannot allocate duplicate sequence numbers."""
    path = tmp_path / "history.db"
    with ContextStore(path) as store:
        store.create_session("a", "scripted", _context())

    def write(worker: int) -> None:
        with ContextStore(path) as store:
            for number in range(10):
                store.append("a", EntryKind.USER, {"worker": worker, "number": number})

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(write, range(3)))
    with ContextStore(path) as store:
        history = store.history("a")
        assert [entry.sequence for entry in history] == list(range(1, 31))
        assert len({json.dumps(entry.payload, sort_keys=True) for entry in history}) == 30


@pytest.mark.parametrize("target", ["context", "context-id", "entry-json", "entry-kind"])
def test_corrupt_persisted_content_fails_explicitly(tmp_path: Path, target: str) -> None:
    """Corruption is reported, not replaced with plausible empty history."""
    path = tmp_path / "history.db"
    with ContextStore(path) as store:
        store.create_session("a", "scripted", _context())
        store.append("a", EntryKind.USER, {})
    with closing(sqlite3.connect(path)) as connection, connection:
        if target == "context":
            connection.execute("UPDATE contexts SET payload = '{}'")
        elif target == "context-id":
            context = json.loads(connection.execute("SELECT payload FROM contexts").fetchone()[0])
            context["context_id"] = "wrong"
            connection.execute("UPDATE contexts SET payload = ?", (json.dumps(context),))
        elif target == "entry-json":
            connection.execute("UPDATE entries SET payload = '{broken'")
        else:
            connection.execute("UPDATE entries SET kind = 'unknown'")
    with ContextStore(path) as store:
        with pytest.raises(StoreError, match="stored"):
            if target.startswith("context"):
                store.get_context("a")
            else:
                store.history("a")


def test_close_is_idempotent_and_context_manager_closes_on_failure(tmp_path: Path) -> None:
    """No connection remains usable after explicit or exceptional closure."""
    store = ContextStore(tmp_path / "history.db")
    with pytest.raises(RuntimeError, match="caller failure"):
        with store:
            store.create_session("a", "scripted", _context())
            raise RuntimeError("caller failure")
    store.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        store.list_sessions()
    with ContextStore(tmp_path / "history.db") as reopened:
        assert reopened.get_session("a").provider_id == "scripted"


def test_io_and_database_failures_are_not_hidden(tmp_path: Path) -> None:
    """An invalid file or missing parent fails instead of using an in-memory fallback."""
    with pytest.raises(sqlite3.OperationalError):
        ContextStore(tmp_path / "missing" / "history.db")
    path = tmp_path / "invalid.db"
    path.write_text("not a SQLite database", encoding="utf-8")
    with pytest.raises(sqlite3.DatabaseError):
        ContextStore(path)
    assert path.read_text(encoding="utf-8") == "not a SQLite database"
