# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Asynchronous child-process supervision."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from .logs import LogStore


class ManagedProcess:
    """Run one process while continuously collecting both output streams."""

    def __init__(self, name: str, command: Sequence[str], logs: LogStore) -> None:
        self.name = name
        self.command = list(command)
        self.logs = logs
        self.process: asyncio.subprocess.Process | None = None
        self._readers: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        """Start the process and its output readers."""
        self.logs.append(self.name, "system", f"starting: {' '.join(self.command)}")
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self._readers = [
            asyncio.create_task(self._read(self.process.stdout, "stdout")),
            asyncio.create_task(self._read(self.process.stderr, "stderr")),
        ]

    async def _read(self, reader: asyncio.StreamReader, stream: str) -> None:
        while line := await reader.readline():
            self.logs.append(self.name, stream, line.decode(errors="replace"))

    async def stop(self) -> None:
        """Terminate the process, escalating to kill after five seconds."""
        if self.process is None or self.process.returncode is not None:
            return
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=5)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()
        await asyncio.gather(*self._readers, return_exceptions=True)
        self.logs.append(self.name, "system", f"exited: {self.process.returncode}")
