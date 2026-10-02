# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""pyGdbServer orchestration and JSON-RPC 2.0 WebSocket API."""

from __future__ import annotations

import asyncio
from importlib.util import find_spec
import json
from pathlib import Path
import re
import socket
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve

from .config import ServerConfig
from .logs import LogEvent, LogStore
from .mi import MiSession
from .process import ManagedProcess


class PyGdbServer:
    """Supervise the debug stack and expose it through JSON-RPC."""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.logs = LogStore(config.log_directory)
        self.ocd: ManagedProcess | None = None
        self.mi = MiSession(config.gdb_path, config.gdb_args, self.logs)
        self.gdb_port = 0
        self.api_port = 0
        self._websocket_server: Server | None = None
        self._shutdown = asyncio.Event()

    async def start(self) -> None:
        """Start OCD and GDB, connect the target, and load pyGdbToolkit."""
        self.gdb_port = _free_loopback_port()
        self.ocd = ManagedProcess("ocd", self.config.ocd_command(self.gdb_port), self.logs)
        await self.ocd.start()
        await self._wait_for_ocd()

        await self.mi.start(self.config.startup_timeout)
        await self.mi.console("set pagination off")
        await self.mi.console("set confirm off")
        await self.mi.console(f"target extended-remote 127.0.0.1:{self.gdb_port}")
        toolkit_path = json.dumps(str(_toolkit_python_path()))
        await self.mi.console(
            f"python import sys; sys.path.insert(0, {toolkit_path}); import pyGdbToolkit"
        )
        for command in self.config.gdb_init:
            await self.mi.console(command)

        self._websocket_server = await serve(
            self._handle_connection,
            self.config.listen_host,
            self.config.listen_port,
            max_size=8 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        )
        socket_info = self._websocket_server.sockets[0].getsockname()
        self.api_port = int(socket_info[1])
        self.logs.append(
            "server",
            "system",
            f"JSON-RPC WebSocket listening on {self.config.listen_host}:{self.api_port}",
        )

    async def _wait_for_ocd(self) -> None:
        deadline = asyncio.get_running_loop().time() + self.config.startup_timeout
        listener_message = _ocd_listener_message(self.config.ocd_path, self.gdb_port)
        while asyncio.get_running_loop().time() < deadline:
            if self.ocd is None or self.ocd.process is None:
                raise RuntimeError("OCD was not started")
            if self.ocd.process.returncode is not None:
                raise RuntimeError(f"OCD exited with status {self.ocd.process.returncode}")
            if listener_message is not None:
                if any(
                    listener_message in str(event["message"])
                    for event in self.logs.get()
                    if event["source"] == "ocd"
                ):
                    return
            else:
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", self.gdb_port)
                except OSError:
                    pass
                else:
                    writer.close()
                    await writer.wait_closed()
                    return
            await asyncio.sleep(0.05)
        raise TimeoutError(f"OCD did not open GDB port {self.gdb_port}")

    async def _handle_connection(self, websocket: ServerConnection) -> None:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1_000)

        def publish_log(event: LogEvent) -> None:
            notification = {
                "jsonrpc": "2.0",
                "method": "log.event",
                "params": event.to_dict(),
            }
            try:
                queue.put_nowait(notification)
            except asyncio.QueueFull:
                pass

        sender = asyncio.create_task(self._send_notifications(websocket, queue))
        subscribed = False
        try:
            async for message in websocket:
                response, subscribe = await self.handle_rpc_message(message)
                if subscribe and not subscribed:
                    self.logs.subscribe(publish_log)
                    subscribed = True
                if response is not None:
                    await websocket.send(json.dumps(response))
                if _is_shutdown_request(message, response):
                    self._shutdown.set()
                    break
        finally:
            if subscribed:
                self.logs.unsubscribe(publish_log)
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    @staticmethod
    async def _send_notifications(
        websocket: ServerConnection, queue: asyncio.Queue[dict[str, Any]]
    ) -> None:
        while True:
            await websocket.send(json.dumps(await queue.get()))

    async def handle_rpc_message(self, message: str | bytes) -> tuple[dict[str, Any] | None, bool]:
        """Validate and dispatch one JSON-RPC message."""
        request_id: object = None
        try:
            if isinstance(message, bytes):
                message = message.decode("utf-8")
            request = json.loads(message)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _rpc_error(None, -32700, "Parse error"), False
        if not isinstance(request, dict):
            return _rpc_error(None, -32600, "Invalid Request"), False
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return _rpc_error(request_id, -32600, "Invalid Request"), False
        has_id = "id" in request
        params = request.get("params", {})
        if not isinstance(params, dict):
            return _rpc_error(request_id, -32602, "Invalid params"), False

        try:
            result, subscribe = await self._dispatch(request["method"], params)
        except KeyError as error:
            response = _rpc_error(request_id, -32602, f"Missing parameter: {error.args[0]}")
            return (response if has_id else None), False
        except RpcMethodNotFound as error:
            response = _rpc_error(request_id, -32601, str(error))
            return (response if has_id else None), False
        except (TypeError, ValueError) as error:
            response = _rpc_error(request_id, -32602, str(error))
            return (response if has_id else None), False
        except Exception as error:
            self.logs.append("server", "error", str(error))
            response = _rpc_error(request_id, -32000, str(error))
            return (response if has_id else None), False

        if "id" not in request:
            return None, subscribe
        return {"jsonrpc": "2.0", "id": request_id, "result": result}, subscribe

    async def _dispatch(
        self, method: str, params: dict[str, Any]
    ) -> tuple[dict[str, Any] | list[dict[str, int | str]], bool]:
        if method == "server.status":
            return self.status(), False
        if method == "command.execute":
            command = params["command"]
            if not isinstance(command, str) or not command.strip():
                raise ValueError("command must be a non-empty string")
            timeout = _timeout(params)
            if command.startswith("gdb "):
                command = command[4:]
            result = await self.mi.console(command, timeout)
            return result.to_dict(), False
        if method == "mi.execute":
            command = params["command"]
            if not isinstance(command, str) or not command.startswith("-"):
                raise ValueError("MI command must be a string starting with '-'")
            return (await self.mi.execute(command, _timeout(params))).to_dict(), False
        if method == "target.status":
            return await self.target_status(), False
        if method == "svd.peripherals":
            return await self.svd_peripherals(), False
        if method == "logs.get":
            since = params.get("since", 0)
            limit = params.get("limit", 1_000)
            if not isinstance(since, int) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
                raise ValueError("since and limit must be integers, with limit between 1 and 10000")
            return self.logs.get(since, limit), False
        if method == "logs.subscribe":
            return {"subscribed": True}, True
        if method == "server.shutdown":
            return {"stopping": True}, False
        raise RpcMethodNotFound(f"Method not found: {method}")

    async def target_status(self) -> dict[str, Any]:
        """Return selected GDB thread/core, execution state, and discovered AP."""
        result = await self.mi.execute("-thread-info")
        state_match = re.search(r'\bstate="([^"]+)"', result.record)
        thread_match = re.search(r'current-thread-id="([^"]+)"', result.record)
        core_match = re.search(r'\bcore="([^"]+)"', result.record)
        if core_match is None:
            core_result = await self.mi.console("monitor core")
            core_match = next(
                (
                    match
                    for output in core_result.output
                    if (match := re.search(r"\bCore\s+(\d+)\b", output, re.IGNORECASE))
                ),
                None,
            )
        ap_names = sorted(
            {
                match.group(1)
                for event in self.logs.get(limit=10_000)
                if event["source"] == "ocd"
                if (match := re.search(r"\b((?:AHB\d+-)?AP#\d+)", str(event["message"])))
            }
        )
        return {
            "state": state_match.group(1) if state_match else "unknown",
            "thread_id": thread_match.group(1) if thread_match else None,
            "core": core_match.group(1) if core_match else None,
            "access_port": ap_names[0] if len(ap_names) == 1 else None,
            "access_ports": ap_names,
        }

    async def svd_peripherals(self) -> dict[str, Any]:
        """Return loaded SVD metadata in a stable JSON shape for client trees."""
        marker = "PYGDBSERVER_SVD_JSON:"
        command = (
            "python import json; from pyGdbToolkit.cmd_svd import SESSION; "
            f"print('{marker}' + json.dumps({{'device': SESSION.device.name if SESSION.device else None, "
            "'peripherals': [{'name': p.name, 'description': p.description, "
            "'base_address': p.base_address, 'registers': [{'name': r.name, "
            "'address_offset': r.address_offset, 'description': r.description} "
            "for r in p.registers]} for p in SESSION.device.peripherals] "
            "if SESSION.device else []}))"
        )
        result = await self.mi.console(command)
        for output in result.output:
            for line in output.splitlines():
                payload_index = line.find(marker)
                if payload_index >= 0:
                    payload = line[payload_index + len(marker) :].strip()
                    data = json.loads(payload)
                    return {"loaded": data["device"] is not None, **data}
        raise RuntimeError("GDB did not return structured SVD metadata")

    def status(self) -> dict[str, Any]:
        """Describe live endpoints and child process state."""
        ocd_process = self.ocd.process if self.ocd is not None else None
        return {
            "ready": self.api_port != 0,
            "api": {"host": self.config.listen_host, "port": self.api_port},
            "ocd": {
                "executable": Path(self.config.ocd_path).name,
                "pid": ocd_process.pid if ocd_process is not None else None,
                "gdb_port": self.gdb_port,
                "running": ocd_process is not None and ocd_process.returncode is None,
            },
            "gdb": {
                "pid": self.mi.process.pid if self.mi.process is not None else None,
                "interpreter": self.mi.interpreter,
                "mi_host": "127.0.0.1",
                "mi_port": self.mi.proxy_port,
                "running": self.mi.process is not None and self.mi.process.returncode is None,
            },
            "log_file": str(self.logs.path),
        }

    async def wait_closed(self) -> None:
        """Wait until a shutdown RPC or an external cancellation."""
        await self._shutdown.wait()

    async def stop(self) -> None:
        """Stop accepting clients, then stop GDB and OCD."""
        if self._websocket_server is not None:
            self._websocket_server.close()
            await self._websocket_server.wait_closed()
        await self.mi.stop()
        if self.ocd is not None:
            await self.ocd.stop()


def _timeout(params: dict[str, Any]) -> float:
    value = params.get("timeout", 30.0)
    if not isinstance(value, (int, float)) or not 0 < value <= 300:
        raise ValueError("timeout must be between 0 and 300 seconds")
    return float(value)


def _rpc_error(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as temporary:
        temporary.bind(("127.0.0.1", 0))
        return int(temporary.getsockname()[1])


def _toolkit_python_path() -> Path:
    spec = find_spec("pyGdbToolkit")
    if spec is None or spec.origin is None:
        raise RuntimeError("cannot locate the pyGdbToolkit installation")
    return Path(spec.origin).resolve().parent.parent


class RpcMethodNotFound(Exception):
    """Signal a JSON-RPC method lookup failure."""


def _ocd_listener_message(executable: str, port: int) -> str | None:
    name = Path(executable).name.lower()
    if "pyocd" in name:
        return f"GDB server listening on port {port}"
    if "openocd" in name:
        return f"Listening on port {port} for gdb connections"
    return None


def _is_shutdown_request(message: str | bytes, response: dict[str, Any] | None) -> bool:
    try:
        if isinstance(message, bytes):
            message = message.decode("utf-8")
        request = json.loads(message)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(request, dict) or request.get("method") != "server.shutdown":
        return False
    return response is None or response.get("result", {}).get("stopping") is True
