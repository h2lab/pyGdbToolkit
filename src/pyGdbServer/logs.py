# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Persistent structured logs shared by processes and network clients."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path


@dataclass(frozen=True)
class LogEvent:
    """One ordered process output event."""

    sequence: int
    timestamp: str
    source: str
    stream: str
    message: str

    def to_dict(self) -> dict[str, int | str]:
        """Return the event's JSON representation."""
        return asdict(self)


class LogStore:
    """Keep recent events in memory and all events in a JSON Lines file."""

    def __init__(self, directory: Path, capacity: int = 10_000) -> None:
        run_name = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        self.run_directory = directory / run_name
        self.run_directory.mkdir(parents=True, exist_ok=False)
        self.path = self.run_directory / "events.jsonl"
        self._events: deque[LogEvent] = deque(maxlen=capacity)
        self._sequence = 0
        self._subscribers: set[Callable[[LogEvent], None]] = set()

    def append(self, source: str, stream: str, message: str) -> LogEvent:
        """Persist and publish one output line."""
        self._sequence += 1
        event = LogEvent(
            sequence=self._sequence,
            timestamp=datetime.now(UTC).isoformat(),
            source=source,
            stream=stream,
            message=message.rstrip("\r\n"),
        )
        self._events.append(event)
        with self.path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        for subscriber in tuple(self._subscribers):
            subscriber(event)
        return event

    def get(self, since: int = 0, limit: int = 1_000) -> list[dict[str, int | str]]:
        """Return recent events after a sequence number."""
        return [event.to_dict() for event in self._events if event.sequence > since][:limit]

    def subscribe(self, callback: Callable[[LogEvent], None]) -> None:
        """Register a synchronous event callback."""
        self._subscribers.add(callback)

    def unsubscribe(self, callback: Callable[[LogEvent], None]) -> None:
        """Remove a previously registered callback."""
        self._subscribers.discard(callback)
