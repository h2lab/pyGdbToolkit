# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Local, provider-independent conversation journal backed by SQLite."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
import json
import math
from pathlib import Path
import sqlite3
from types import TracebackType

from .context import parse_target_context
from .contracts import JsonObject, JsonValue, TargetContext

_APPLICATION_ID = 0x5047444C
_SCHEMA_VERSION = 1
_SCHEMA = (
    """CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY,
        provider_id TEXT NOT NULL,
        title TEXT NOT NULL,
        context_id TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE contexts (
        session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
        context_id TEXT NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (session_id, context_id)
    )""",
    """CREATE TABLE entries (
        session_id TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK (sequence > 0),
        context_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (session_id, sequence),
        FOREIGN KEY (session_id, context_id)
            REFERENCES contexts(session_id, context_id) ON DELETE CASCADE
    )""",
)


class StoreError(RuntimeError):
    """The database schema or stored conversation data is incompatible or invalid."""


class EntryKind(StrEnum):
    """Kinds of observations stored locally, never executable instructions."""

    USER = "user"
    ASSISTANT = "assistant"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SUMMARY = "summary"
    ERROR = "error"


@dataclass(frozen=True)
class StoredSession:
    """Conversation metadata, including the most recently registered target context."""

    session_id: str
    provider_id: str
    title: str
    context_id: str
    created_at: str


@dataclass(frozen=True)
class HistoryEntry:
    """One ordered observation bound to a specific target snapshot."""

    sequence: int
    context_id: str
    kind: EntryKind
    payload: JsonObject
    created_at: str


def _identifier(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _json_value(item) for key, item in value.items()}
    raise ValueError("history payloads require JSON values and string object keys")


def _payload(value: object) -> JsonObject:
    result = _json_value(value)
    if not isinstance(result, dict):
        raise ValueError("history payload must be a JSON object")
    return result


def _encode(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _context_payload(context: TargetContext) -> str:
    data = asdict(context)
    data["memory_map"]["regions"] = list(data["memory_map"]["regions"])
    parse_target_context(data)
    return _encode(data)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _page(limit: int, offset: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer between 1 and 1000")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")


class ContextStore:
    """Persist sessions, target snapshots and ordered history without target access.

    The synchronous connection belongs to its creating thread. Separate store
    instances may share a file; SQLite serializes their write transactions.
    Callers supply the path and must protect the containing directory.
    """

    def __init__(self, path: str | Path) -> None:
        """Open a database, creating schema version 1 only in an empty database."""
        self._connection = sqlite3.connect(path, isolation_level="IMMEDIATE")
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._initialize()
        except (sqlite3.Error, StoreError):
            self._connection.close()
            raise

    def _initialize(self) -> None:
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            application = self._connection.execute("PRAGMA application_id").fetchone()[0]
            if version == _SCHEMA_VERSION and application == _APPLICATION_ID:
                return
            tables = self._connection.execute(
                "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if version != 0 or application != 0 or tables:
                raise StoreError(
                    f"unsupported conversation database: application={application}, version={version}"
                )
            for statement in _SCHEMA:
                self._connection.execute(statement)
            self._connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
            self._connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def __enter__(self) -> ContextStore:
        """Return the store, whose connection is closed on leaving the block."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the store without suppressing any exception."""
        self.close()

    def close(self) -> None:
        """Close the connection idempotently; later operations fail explicitly."""
        self._connection.close()

    def create_session(
        self, session_id: str, provider_id: str, context: TargetContext, title: str = ""
    ) -> StoredSession:
        """Atomically register a new conversation and its initial target snapshot."""
        _identifier(session_id, "session_id")
        _identifier(provider_id, "provider_id")
        if not isinstance(title, str):
            raise ValueError("title must be a string")
        payload = _context_payload(context)
        created_at = _now()
        with self._connection:
            self._connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                (session_id, provider_id, title, context.context_id, created_at),
            )
            self._connection.execute(
                "INSERT INTO contexts VALUES (?, ?, ?)", (session_id, context.context_id, payload)
            )
        return StoredSession(session_id, provider_id, title, context.context_id, created_at)

    def get_session(self, session_id: str) -> StoredSession:
        """Return conversation metadata or raise KeyError for an unknown session."""
        row = self._connection.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown session: {session_id}")
        return StoredSession(**dict(row))

    def list_sessions(self, limit: int = 100, offset: int = 0) -> tuple[StoredSession, ...]:
        """List conversations in creation order, with bounded pagination."""
        _page(limit, offset)
        rows = self._connection.execute(
            "SELECT * FROM sessions ORDER BY created_at, session_id LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return tuple(StoredSession(**dict(row)) for row in rows)

    def save_context(self, session_id: str, context: TargetContext) -> None:
        """Register and select a snapshot, rejecting reuse of an ID for different facts."""
        payload = _context_payload(context)
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self.get_session(session_id)
            row = self._connection.execute(
                "SELECT payload FROM contexts WHERE session_id = ? AND context_id = ?",
                (session_id, context.context_id),
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO contexts VALUES (?, ?, ?)",
                    (session_id, context.context_id, payload),
                )
            elif row["payload"] != payload:
                raise StoreError("target context ID already exists with different facts")
            self._connection.execute(
                "UPDATE sessions SET context_id = ? WHERE session_id = ?",
                (context.context_id, session_id),
            )

    def get_context(self, session_id: str, context_id: str | None = None) -> TargetContext:
        """Read a registered snapshot, defaulting to the session's current context."""
        session = self.get_session(session_id)
        selected = session.context_id if context_id is None else context_id
        row = self._connection.execute(
            "SELECT payload FROM contexts WHERE session_id = ? AND context_id = ?",
            (session_id, selected),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown target context for session {session_id}: {selected}")
        try:
            context = parse_target_context(json.loads(row["payload"]))
        except (ValueError, TypeError) as error:
            raise StoreError("invalid stored target context") from error
        if context.context_id != selected:
            raise StoreError("stored target context ID does not match its key")
        return context

    def append(
        self,
        session_id: str,
        kind: EntryKind,
        payload: JsonObject,
        context_id: str | None = None,
    ) -> HistoryEntry:
        """Commit one observation against a registered snapshot, with a session-local sequence."""
        if not isinstance(kind, EntryKind):
            raise ValueError("kind must be an EntryKind")
        encoded = _encode(_payload(payload))
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            context = self.get_context(session_id, context_id)
            sequence = self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM entries WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            created_at = _now()
            self._connection.execute(
                "INSERT INTO entries VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, sequence, context.context_id, kind.value, encoded, created_at),
            )
        return HistoryEntry(
            sequence, context.context_id, kind, _payload(json.loads(encoded)), created_at
        )

    def history(
        self, session_id: str, after_sequence: int = 0, limit: int = 100
    ) -> tuple[HistoryEntry, ...]:
        """Read observations in sequence order, using a bounded exclusive cursor."""
        _page(limit, after_sequence)
        self.get_session(session_id)
        rows = self._connection.execute(
            """SELECT * FROM entries WHERE session_id = ? AND sequence > ?
               ORDER BY sequence LIMIT ?""",
            (session_id, after_sequence, limit),
        )
        try:
            return tuple(
                HistoryEntry(
                    row["sequence"],
                    row["context_id"],
                    EntryKind(row["kind"]),
                    _payload(json.loads(row["payload"])),
                    row["created_at"],
                )
                for row in rows
            )
        except (ValueError, TypeError) as error:
            raise StoreError("invalid stored history entry") from error

    def delete_session(self, session_id: str) -> None:
        """Atomically delete a conversation and its snapshots and history."""
        with self._connection:
            cursor = self._connection.execute(
                "DELETE FROM sessions WHERE session_id = ?", (session_id,)
            )
            if cursor.rowcount == 0:
                raise KeyError(f"unknown session: {session_id}")
