# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""GDB Machine Interface session and private loopback endpoint."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from typing import Any

from .logs import LogStore


@dataclass(frozen=True)
class MiResult:
    """Result record and console streams emitted for one MI command."""

    result_class: str
    record: str
    output: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable command result."""
        return {"class": self.result_class, "record": self.record, "output": list(self.output)}


class MiSession:
    """Own GDB and serialize commands over MI3, falling back to MI2."""

    def __init__(self, gdb_path: str, gdb_args: tuple[str, ...], logs: LogStore) -> None:
        self.gdb_path = gdb_path
        self.gdb_args = gdb_args
        self.logs = logs
        self.process: asyncio.subprocess.Process | None = None
        self.interpreter = ""
        self.proxy_port = 0
        self._reader_task: asyncio.Task[None] | None = None
        self._proxy: asyncio.Server | None = None
        self._proxy_clients: set[asyncio.StreamWriter] = set()
        self._ready = asyncio.Event()
        self._token = 0
        self._pending: dict[int, asyncio.Future[MiResult]] = {}
        self._active_output: list[str] | None = None
        self._lock = asyncio.Lock()

    async def start(self, timeout: float) -> None:
        """Start GDB and expose its MI stream on a dynamic loopback port."""
        for interpreter in ("mi3", "mi2"):
            if await self._start_interpreter(interpreter, timeout):
                self.interpreter = interpreter
                self._proxy = await asyncio.start_server(self._handle_proxy, "127.0.0.1", 0)
                socket = self._proxy.sockets[0]
                self.proxy_port = int(socket.getsockname()[1])
                self.logs.append(
                    "gdb", "system", f"private {interpreter} endpoint: 127.0.0.1:{self.proxy_port}"
                )
                return
            await self.stop()
        raise RuntimeError("GDB supports neither MI3 nor MI2")

    async def _start_interpreter(self, interpreter: str, timeout: float) -> bool:
        self._ready = asyncio.Event()
        command = [
            self.gdb_path,
            "--quiet",
            "--nx",
            f"--interpreter={interpreter}",
            *self.gdb_args,
        ]
        self.logs.append("gdb", "system", f"starting: {' '.join(command)}")
        self.process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=8 * 1024 * 1024,
        )
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self._reader_task = asyncio.create_task(self._read_stdout(self.process.stdout))
        asyncio.create_task(self._read_stderr(self.process.stderr))
        ready_task = asyncio.create_task(self._ready.wait())
        exit_task = asyncio.create_task(self.process.wait())
        try:
            done, pending = await asyncio.wait(
                (ready_task, exit_task), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        except asyncio.CancelledError:
            ready_task.cancel()
            exit_task.cancel()
            raise
        if not done or exit_task in done:
            return False
        return self.process.returncode is None

    async def _read_stdout(self, reader: asyncio.StreamReader) -> None:
        try:
            while line_bytes := await reader.readline():
                line = line_bytes.decode(errors="replace").rstrip("\r\n")
                self.logs.append("gdb", "mi", line)
                self._broadcast_proxy(line_bytes)
                if line.startswith("(gdb)"):
                    self._ready.set()
                    continue
                if line[:1] in {"~", "@", "&"} and self._active_output is not None:
                    self._active_output.append(_decode_mi_string(line[1:]))
                    continue
                token_text, separator, record = line.partition("^")
                if separator and token_text.isdecimal():
                    future = self._pending.pop(int(token_text), None)
                    if future is not None and not future.done():
                        result_class = record.split(",", 1)[0]
                        future.set_result(
                            MiResult(result_class, record, tuple(self._active_output or ()))
                        )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.logs.append("gdb", "error", f"MI reader failed: {error}")
            failure = RuntimeError(f"GDB MI reader failed: {error}")
        else:
            failure = RuntimeError("GDB MI stream closed")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(failure)
        self._pending.clear()

    async def _read_stderr(self, reader: asyncio.StreamReader) -> None:
        while line := await reader.readline():
            self.logs.append("gdb", "stderr", line.decode(errors="replace"))

    def _broadcast_proxy(self, data: bytes) -> None:
        for writer in tuple(self._proxy_clients):
            if writer.is_closing():
                self._proxy_clients.discard(writer)
            else:
                writer.write(data)

    async def _handle_proxy(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._proxy_clients.add(writer)
        try:
            while data := await reader.readline():
                if self.process is None or self.process.stdin is None:
                    break
                self.process.stdin.write(data)
                await self.process.stdin.drain()
        finally:
            self._proxy_clients.discard(writer)
            writer.close()
            await writer.wait_closed()

    async def execute(self, command: str, timeout: float = 30.0) -> MiResult:
        """Execute one raw MI command and await its result record."""
        async with self._lock:
            if (
                self.process is None
                or self.process.stdin is None
                or self.process.returncode is not None
            ):
                raise RuntimeError("GDB is not running")
            self._token += 1
            token = self._token
            loop = asyncio.get_running_loop()
            future: asyncio.Future[MiResult] = loop.create_future()
            self._pending[token] = future
            self._active_output = []
            self.process.stdin.write(f"{token}{command}\n".encode())
            await self.process.stdin.drain()
            try:
                result = await asyncio.wait_for(future, timeout)
            finally:
                self._pending.pop(token, None)
                self._active_output = None
            if result.result_class == "error":
                raise RuntimeError(result.record)
            return result

    async def console(self, command: str, timeout: float = 30.0) -> MiResult:
        """Execute a CLI command through MI's console interpreter."""
        encoded = json.dumps(command)
        return await self.execute(f"-interpreter-exec console {encoded}", timeout)

    async def stop(self) -> None:
        """Close the private endpoint and terminate GDB."""
        if self._proxy is not None:
            self._proxy.close()
            await self._proxy.wait_closed()
            self._proxy = None
        for writer in tuple(self._proxy_clients):
            writer.close()
        self._proxy_clients.clear()
        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self._reader_task is not None:
            await asyncio.gather(self._reader_task, return_exceptions=True)


def _decode_mi_string(value: str) -> str:
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return value

    escapes = {
        "a": "\a",
        "b": "\b",
        "e": "\x1b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "\\": "\\",
        '"': '"',
    }
    body = value[1:-1]
    decoded = bytearray()
    index = 0
    while index < len(body):
        character = body[index]
        index += 1
        if character != "\\" or index == len(body):
            decoded.extend(character.encode("utf-8"))
            continue

        escaped = body[index]
        index += 1
        if escaped in escapes:
            decoded.extend(escapes[escaped].encode("utf-8"))
        elif escaped == "x":
            start = index
            while (
                index < len(body) and index - start < 2 and body[index] in "0123456789abcdefABCDEF"
            ):
                index += 1
            decoded.append(int(body[start:index], 16) if index > start else ord("x"))
        elif escaped in "01234567":
            digits = escaped
            while index < len(body) and len(digits) < 3 and body[index] in "01234567":
                digits += body[index]
                index += 1
            decoded.append(int(digits, 8))
        else:
            decoded.extend(escaped.encode("utf-8"))
    return decoded.decode("utf-8", errors="replace")
