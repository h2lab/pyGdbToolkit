# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Local, provider-independent conversation journal backed by SQLite."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum, auto
import json
import math
from pathlib import Path
import sqlite3
from types import TracebackType
from typing import Self
from uuid import uuid4

from .context import parse_target_context
from .contracts import JsonObject, JsonValue, TargetContext

_APPLICATION_ID = 0x5047444C
_SCHEMA_VERSION = 2
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
_AUDIT_SCHEMA = (
    """CREATE TABLE audits (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, started_at TEXT NOT NULL,
        stopped_at TEXT, writer TEXT NOT NULL
    )""",
    """CREATE TABLE audit_events (
        audit_id TEXT NOT NULL REFERENCES audits(id),
        sequence INTEGER NOT NULL, created_at TEXT NOT NULL, kind TEXT NOT NULL,
        payload TEXT NOT NULL, context_key TEXT,
        PRIMARY KEY (audit_id, sequence), UNIQUE (audit_id, context_key)
    )""",
)


class StoreError(RuntimeError):
    """The database schema or stored conversation data is incompatible or invalid."""


class EntryKind(StrEnum):
    """Kinds of observations stored locally, never executable instructions."""

    USER = auto()
    ASSISTANT = auto()
    TOOL_CALL = auto()
    TOOL_RESULT = auto()
    SUMMARY = auto()
    ERROR = auto()


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
        """Open or migrate a conversation database."""
        self._active_audit: str | None = None
        self._writer = uuid4().hex
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
            if version == 1 and application == _APPLICATION_ID:
                for statement in _AUDIT_SCHEMA:
                    self._connection.execute(statement)
                self._connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                return
            tables = self._connection.execute(
                "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if version != 0 or application != 0 or tables:
                raise StoreError(
                    f"unsupported conversation database: application={application}, version={version}"
                )
            for statement in (*_SCHEMA, *_AUDIT_SCHEMA):
                self._connection.execute(statement)
            self._connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
            self._connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def __enter__(self) -> Self:
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

    @property
    def active_audit(self) -> str | None:
        """Return the dossier attached to this client run."""
        return self._active_audit

    def start_audit(self, title: str, snapshot: JsonObject) -> str:
        """Open a dossier and commit its initial snapshot atomically."""
        if self._active_audit is not None:
            raise StoreError("an audit is already active")
        audit_id = uuid4().hex
        with self._connection:
            self._connection.execute(
                "INSERT INTO audits VALUES (?, ?, ?, NULL, ?)",
                (audit_id, title, _now(), self._writer),
            )
            self._audit_event(audit_id, "snapshot", {"stage": "initial", "data": snapshot})
        self._active_audit = audit_id
        return audit_id

    def resume_audit(self, audit_id: str, snapshot: JsonObject) -> None:
        """Attach an open dossier and mark the unobserved interval."""
        if self._active_audit is not None:
            raise StoreError("an audit is already active")
        with self._connection:
            updated = self._connection.execute(
                "UPDATE audits SET writer = ? WHERE id = ? AND stopped_at IS NULL",
                (self._writer, audit_id),
            )
            if updated.rowcount != 1:
                raise StoreError(f"no open audit with ID {audit_id}")
            self._audit_event(audit_id, "gap", {"reason": "collection interrupted before resume"})
            self._audit_event(audit_id, "snapshot", {"stage": "resumed", "data": snapshot})
        self._active_audit = audit_id

    def stop_audit(self, snapshot: JsonObject) -> str:
        """Commit a final snapshot and close the active dossier."""
        audit_id = self._active_audit
        if audit_id is None:
            raise StoreError("no audit is active")
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            self._audit_event(audit_id, "snapshot", {"stage": "final", "data": snapshot})
            self._connection.execute(
                "UPDATE audits SET stopped_at = ? WHERE id = ?", (_now(), audit_id)
            )
        self._active_audit = None
        return audit_id

    def record_audit(self, kind: str, payload: JsonObject) -> None:
        """Append a client observation to the active dossier, if any."""
        if self._active_audit is not None:
            with self._connection:
                self._connection.execute("BEGIN IMMEDIATE")
                self._audit_event(self._active_audit, kind, payload)

    def _audit_event(
        self, audit_id: str, kind: str, payload: JsonObject, context_key: str | None = None
    ) -> None:
        updated = self._connection.execute(
            """INSERT INTO audit_events
               SELECT a.id, COALESCE((SELECT MAX(e.sequence) FROM audit_events e
                   WHERE e.audit_id = a.id), 0) + 1, ?, ?, ?, ?
               FROM audits a WHERE a.id = ? AND a.writer = ? AND a.stopped_at IS NULL
               ON CONFLICT(audit_id, context_key) DO NOTHING""",
            (_now(), kind, _encode(payload), context_key, audit_id, self._writer),
        )
        if updated.rowcount == 0:
            owner = self._connection.execute(
                "SELECT 1 FROM audits WHERE id = ? AND writer = ? AND stopped_at IS NULL",
                (audit_id, self._writer),
            ).fetchone()
            if owner is None:
                raise StoreError("audit is no longer attached to this client")

    def read_audit(self, audit_id: str | None = None) -> tuple[JsonObject, list[JsonObject]]:
        """Read a consistent dossier, defaulting to active or most recently closed."""
        selected = self._active_audit if audit_id is None else audit_id
        with self._connection:
            self._connection.execute("BEGIN")
            if selected is None:
                row = self._connection.execute(
                    """SELECT id, title, started_at, stopped_at FROM audits
                       WHERE stopped_at IS NOT NULL ORDER BY stopped_at DESC, id LIMIT 1"""
                ).fetchone()
            else:
                row = self._connection.execute(
                    "SELECT id, title, started_at, stopped_at FROM audits WHERE id = ?", (selected,)
                ).fetchone()
            if row is None:
                raise StoreError("no matching audit dossier")
            events = self._connection.execute(
                """SELECT sequence, created_at, kind, payload FROM audit_events
                   WHERE audit_id = ? ORDER BY sequence""",
                (row["id"],),
            )
            return dict(row), [
                {**dict(event), "payload": _payload(json.loads(event["payload"]))}
                for event in events
            ]

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
            if self._active_audit is not None:
                session = self.get_session(session_id)
                self._audit_event(
                    self._active_audit,
                    "llm_context",
                    {
                        "session_id": session_id,
                        "provider": session.provider_id,
                        "session_created_at": session.created_at,
                        "context_id": context.context_id,
                        "data": _payload(json.loads(_context_payload(context))),
                    },
                    _encode([session_id, context.context_id]),
                )
                self._audit_event(
                    self._active_audit,
                    f"llm_{kind.value}",
                    {
                        "session_id": session_id,
                        "context_id": context.context_id,
                        "entry_sequence": sequence,
                        "data": _payload(json.loads(encoded)),
                    },
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
