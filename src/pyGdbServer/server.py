# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""pyGdbServer orchestration and JSON-RPC 2.0 WebSocket API."""

import asyncio
import base64
import binascii
from importlib.util import find_spec
import json
from pathlib import Path
import re
import socket
from tempfile import NamedTemporaryFile
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve

from .config import ServerConfig
from .logs import LogEvent, LogStore
from .ocd import create_mi_session
from .process import ManagedProcess
from .rtos import RtosProjects
from .target_context import build_target_context

_WORKSPACE_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
_WORKSPACE_FILENAME_MAX_LENGTH = 128
_RTOS_RPC_PARAMETERS = {
    "rtos.project.begin": {"name", "rtos"},
    "rtos.project.put": {"name", "path", "offset", "size", "content"},
    "rtos.project.commit": {"name", "timeout"},
    "rtos.project.abort": {"name"},
    "rtos.status": {"timeout"},
}


def _workspace_filename(filename: object) -> str:
    """Validate a workspace file name.

    Parameters
    ----------
    filename : object
        Candidate file name supplied by the client.

    Returns
    -------
    str
        The validated file name.

    Raises
    ------
    ValueError
        If the name is not a string, is empty, contains directory
        separators or components (including ``.`` and ``..``), or exceeds
        128 characters.
    """
    if (
        not isinstance(filename, str)
        or not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or Path(filename).name != filename
    ):
        raise ValueError("filename must be a file name without directory components")
    if len(filename) > _WORKSPACE_FILENAME_MAX_LENGTH:
        raise ValueError(f"filename must not exceed {_WORKSPACE_FILENAME_MAX_LENGTH} characters")
    return filename


class PyGdbServer:
    """Supervise the debug stack and expose it through JSON-RPC."""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.workspace = Path.cwd().resolve()
        self.rtos_projects = RtosProjects(self.workspace)
        self.logs = LogStore(config.log_directory)
        self.ocd: ManagedProcess | None = None
        self.core_ocds: dict[int, ManagedProcess] = {}
        self.core_endpoints: dict[int, str] = {}
        self.mi = create_mi_session(config, self.logs)
        self.gdb_port = 0
        self.telnet_port = 0
        self.swo_port = 0
        self.api_port = 0
        self._status_core_discovery_failed = False
        self._websocket_server: Server | None = None
        self._shutdown = asyncio.Event()

    async def start(self) -> None:
        """Start OCD and GDB, connect the target, and load pyGdbToolkit."""
        if self.config.backend_identifier == "jlinkgdbserver":
            self.gdb_port, self.telnet_port, self.swo_port = _free_jlink_ports()
        else:
            self.gdb_port, self.telnet_port = _free_loopback_ports()
        cluster = self.config.jlink_cluster()
        ocd_command = self.config.ocd_command(
            self.gdb_port, self.telnet_port, self.swo_port or None
        )
        if cluster:
            initial_device = self.config.ocd_args[self.config.ocd_args.index("-device") + 1]
            initial_core = next(
                core for core, device in cluster.items() if device == initial_device
            )
            ocd_command = self.config.jlink_core_command(
                initial_device, self.gdb_port, self.telnet_port, self.swo_port
            )
            self.core_endpoints[initial_core] = f"127.0.0.1:{self.gdb_port}"
        self.ocd = ManagedProcess("ocd", ocd_command, self.logs)
        await self.ocd.start()
        await self._wait_for_ocd()
        for core, device in cluster.items():
            if core in self.core_endpoints:
                continue
            port, telnet, swo = _free_jlink_ports()
            process = ManagedProcess(
                f"ocd-core-{core}",
                self.config.jlink_core_command(device, port, telnet, swo),
                self.logs,
            )
            self.core_ocds[core] = process
            await process.start()
            await self._wait_for_ocd(process, port)
            self.core_endpoints[core] = f"127.0.0.1:{port}"

        await self.mi.start(self.config.startup_timeout)
        await self.mi.console("set pagination off")
        await self.mi.console("set confirm off")
        connection_type = self.config.gdb_connection_type()
        await self.mi.console(f"target {connection_type} 127.0.0.1:{self.gdb_port}")
        toolkit_path = json.dumps(str(_toolkit_python_path()))
        await self.mi.console(
            f"python import sys; sys.path.insert(0, {toolkit_path}); import pyGdbToolkit"
        )
        for command in self.config.gdb_init:
            await self.mi.console(command)
        await self._register_jlink_core()

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

    async def _register_jlink_core(self) -> None:
        """Register supervised endpoint metadata or a detected attached-core identity."""
        if self.config.backend_identifier != "jlinkgdbserver":
            return
        if self.config.jlink_core_devices:
            device = self.config.ocd_args[self.config.ocd_args.index("-device") + 1]
            devices = self.config.jlink_cluster()
            initial = next(core for core, name in devices.items() if name == device)
            await self.mi.console(
                "python from pyGdbToolkit.core_runtime import CORES; "
                f"CORES.register_jlink_cluster({self.core_endpoints!r}, {initial}, {devices!r})"
            )
            self._status_core_discovery_failed = False
            return
        messages = [str(event["message"]) for event in self.logs.get() if event["source"] == "ocd"]
        name = _jlink_core_name(messages)
        if name is None:
            self.logs.append(
                "server",
                "system",
                "J-Link attached Cortex-M identity unavailable in connection logs",
            )
            return
        await self.mi.console(
            "python from pyGdbToolkit.core_runtime import CORES; "
            f"CORES.register_jlink_core({json.dumps(name)})"
        )
        self._status_core_discovery_failed = False

    async def _wait_for_ocd(
        self, process: ManagedProcess | None = None, port: int | None = None
    ) -> None:
        process = self.ocd if process is None else process
        port = self.gdb_port if port is None else port
        source = "ocd" if process is self.ocd else process.name if process is not None else "ocd"
        deadline = asyncio.get_running_loop().time() + self.config.startup_timeout
        listener_message = _ocd_listener_message(self.config.backend_identifier, port)
        is_jlink = self.config.backend_identifier == "jlinkgdbserver"
        while asyncio.get_running_loop().time() < deadline:
            if process is None or process.process is None:
                raise RuntimeError("OCD was not started")
            messages = [
                str(event["message"]) for event in self.logs.get() if event["source"] == source
            ]
            if is_jlink:
                failures = [
                    message
                    for message in messages
                    if message.startswith(
                        ("ERROR:", "Could not connect to target.", "Target connection failed.")
                    )
                ]
                if failures:
                    warnings = [
                        message
                        for message in messages
                        if "Identified core does not match configuration" in message
                    ]
                    raise RuntimeError(f"J-Link startup failed: {'; '.join(warnings + failures)}")
            if process.process.returncode is not None:
                raise RuntimeError(f"OCD exited with status {process.process.returncode}")
            if listener_message is not None:
                if any(listener_message in message for message in messages):
                    return
            else:
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", port)
                except OSError:
                    pass
                else:
                    writer.close()
                    await writer.wait_closed()
                    return
            await asyncio.sleep(0.05)
        raise TimeoutError(f"OCD did not become ready on GDB port {port}")

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
        if method in _RTOS_RPC_PARAMETERS and set(params) - _RTOS_RPC_PARAMETERS[method]:
            raise ValueError(f"Unexpected parameters for {method}")
        if method == "server.status":
            return self.status(), False
        if method == "workspace.upload":
            return await asyncio.to_thread(self._upload_file, params), False
        if method == "workspace.download":
            return await asyncio.to_thread(self._download_file, params), False
        if method == "workspace.list":
            return await asyncio.to_thread(self._list_directory, params), False
        if method == "workspace.erase":
            return await asyncio.to_thread(self._erase_file, params), False
        if method == "rtos.project.begin":
            return (
                await asyncio.to_thread(self.rtos_projects.begin, params["name"], params["rtos"]),
                False,
            )
        if method == "rtos.project.put":
            return (
                await asyncio.to_thread(
                    self.rtos_projects.put,
                    params["name"],
                    params["path"],
                    params["offset"],
                    params["size"],
                    params["content"],
                ),
                False,
            )
        if method == "rtos.project.abort":
            return await asyncio.to_thread(self.rtos_projects.abort, params["name"]), False
        if method == "rtos.project.commit":
            return await self.rtos_project_commit(params["name"], _timeout(params)), False
        if method == "rtos.status":
            return await self.rtos_status(_timeout(params)), False
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
        if method in ("target.cores", "target.core", "target.select_core"):
            core_id = params["core"] if method == "target.select_core" else None
            if method == "target.select_core" and (
                isinstance(core_id, bool) or not isinstance(core_id, int) or core_id < 0
            ):
                raise ValueError("core must be a non-negative integer")
            return await self.target_cores(method, core_id, _timeout(params)), False
        if method == "target.context":
            return await self.target_context(_timeout(params)), False
        if method == "svd.peripherals":
            return await self.svd_peripherals(), False
        if method == "toolkit.commands":
            return await self.toolkit_commands(), False
        if method == "toolkit.help":
            command = params.get("command")
            if command is not None and not isinstance(command, str):
                raise ValueError("command must be a string")
            return await self.toolkit_help(command), False
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

    def _upload_file(self, params: dict[str, Any]) -> dict[str, Any]:
        if "directory" in params:
            raise ValueError("uploads are restricted to the server workspace")
        filename = _workspace_filename(params["filename"])
        content = params["content"]
        if not isinstance(content, str):
            raise ValueError("content must be a base64 string")
        if len(content) > 4 * ((5 * 1024 * 1024 + 2) // 3):
            raise ValueError("upload size must not exceed 5 MiB")
        try:
            data = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("content must be valid base64") from error
        if len(data) > 5 * 1024 * 1024:
            raise ValueError("upload size must not exceed 5 MiB")
        directory = self.workspace
        destination = directory / filename
        with NamedTemporaryFile(dir=directory, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            try:
                temporary.write(data)
                temporary.close()
                temporary_path.replace(destination)
            finally:
                temporary_path.unlink(missing_ok=True)
        return {"path": str(destination), "size": len(data)}

    def _download_file(self, params: dict[str, Any]) -> dict[str, Any]:
        if "directory" in params:
            raise ValueError("downloads are restricted to the server workspace")
        filename = _workspace_filename(params.get("filename"))
        offset = params.get("offset")
        chunk_size = params.get("chunk_size")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if (
            isinstance(chunk_size, bool)
            or not isinstance(chunk_size, int)
            or not 1 <= chunk_size <= _WORKSPACE_DOWNLOAD_CHUNK_SIZE
        ):
            raise ValueError(f"chunk_size must be between 1 and {_WORKSPACE_DOWNLOAD_CHUNK_SIZE}")

        path = (self.workspace / filename).resolve()
        if not path.is_relative_to(self.workspace):
            raise ValueError("download file must be within the server workspace")
        if not path.is_file():
            raise FileNotFoundError(f"Not a file: {path}")

        size = path.stat().st_size
        if offset > size:
            raise ValueError("offset must not exceed the file size")
        expected_size = min(chunk_size, size - offset)
        with path.open("rb") as source:
            source.seek(offset)
            content = source.read(expected_size)
        if len(content) != expected_size:
            raise OSError(f"File changed while downloading: {path}")
        return {
            "filename": filename,
            "offset": offset,
            "size": size,
            "content": base64.b64encode(content).decode("ascii"),
        }

    def _erase_file(self, params: dict[str, Any]) -> dict[str, Any]:
        if set(params) - {"filename", "timeout"}:
            raise ValueError("workspace.erase accepts only filename and timeout")
        filename = _workspace_filename(params.get("filename"))
        path = self.workspace / filename
        path.unlink()
        return {"path": str(path)}

    def _list_directory(self, params: dict[str, Any]) -> dict[str, Any]:
        if params:
            raise ValueError("workspace.list does not accept parameters")
        directory = self.workspace
        entries = [
            {"name": entry.name, "is_directory": entry.is_dir()}
            for entry in sorted(directory.iterdir(), key=lambda entry: entry.name)
        ]
        return {"path": str(directory), "entries": entries}

    async def target_status(self) -> dict[str, Any]:
        """Return selected GDB thread/core, execution state, and discovered AP."""
        result = await self.mi.execute("-thread-info")
        state_match = re.search(r'\bstate="([^"]+)"', result.record)
        thread_match = re.search(r'current-thread-id="([^"]+)"', result.record)
        core_match = re.search(r'\bcore="([^"]+)"', result.record)
        selected_core = None
        if not getattr(self, "_status_core_discovery_failed", False):
            try:
                selected_core = str((await self.target_cores("target.core"))["core"]["id"])
            except RuntimeError:
                self._status_core_discovery_failed = True
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
            "core": (
                selected_core
                if selected_core is not None
                else core_match.group(1) if core_match else None
            ),
            "access_port": ap_names[0] if len(ap_names) == 1 else None,
            "access_ports": ap_names,
        }

    async def target_cores(
        self, method: str, core_id: int | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        """Execute discovery or selection atomically in the GDB-side core runtime."""
        marker = "PYGDBSERVER_CORE_JSON:"
        if method == "target.cores":
            expression = "{'cores': [core.to_dict() for core in CORES.list()]}"
        elif method == "target.select_core":
            expression = f"{{'core': CORES.select({core_id}).to_dict()}}"
        else:
            expression = "{'core': CORES.current().to_dict()}"
        command = (
            "python import json; from pyGdbToolkit.core_runtime import CORES; "
            f"print('{marker}' + json.dumps({expression}))"
        )
        data = await self._python_json(marker, command, timeout)
        if not isinstance(data, dict):
            raise RuntimeError("GDB did not return structured CPU core metadata")
        return data

    async def target_context(self, timeout: float = 30.0) -> dict[str, Any]:
        """Return the versioned target snapshot without probing the target."""
        marker = "PYGDBSERVER_TARGET_CONTEXT_JSON:"
        command = (
            "python import json; "
            "from pyGdbToolkit.target_context import collect_target_context; "
            f"print('{marker}' + json.dumps(collect_target_context()))"
        )
        try:
            facts = await self._python_json(marker, command, timeout)
        except (RuntimeError, ValueError) as error:
            return build_target_context(self.config, None, f"GDB context unavailable: {error}")
        return build_target_context(self.config, facts)

    async def rtos_status(self, timeout: float = 30.0) -> dict[str, Any]:
        """Read the actual GDB RTOS state, including projects loaded through the CLI."""
        marker = "PYGDBSERVER_RTOS_JSON:"
        command = (
            "python import json; from pyGdbToolkit.cmd_rtos import rtos_status; "
            f"print('{marker}' + json.dumps(rtos_status()))"
        )
        data = await self._python_json(marker, command, timeout)
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise RuntimeError("GDB did not return structured RTOS session state")
        return data

    async def rtos_project_commit(self, name: object, timeout: float = 30.0) -> dict[str, Any]:
        """Validate staging and select/load/snapshot the project in one GDB operation."""
        rtos, path = await asyncio.to_thread(self.rtos_projects.prepare, name)
        success = False
        try:
            marker = "PYGDBSERVER_RTOS_JSON:"
            command = (
                "python import json; from pyGdbToolkit.cmd_rtos import load_rtos_project; "
                f"print('{marker}' + json.dumps(load_rtos_project({rtos!r}, {str(path)!r})))"
            )
            data = await self._python_json(marker, command, timeout)
            if (
                not isinstance(data, dict)
                or data.get("schema_version") != 1
                or data.get("loaded") is not True
                or data.get("selected_rtos") != rtos
                or data.get("project_path") != str(path)
            ):
                raise RuntimeError("GDB did not confirm loading the staged RTOS project")
            success = True
            return data
        finally:
            self.rtos_projects.finish(name, success)

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
        data = await self._python_json(marker, command)
        if data is None:
            raise RuntimeError("GDB did not return structured SVD metadata")
        return {"loaded": data["device"] is not None, **data}

    async def toolkit_commands(self) -> dict[str, Any]:
        """Return the name and summary of every pyGdbToolkit command loaded in GDB."""
        commands = await self._toolkit_command_helps()
        return {
            "commands": [
                {"name": command["name"], "summary": command["summary"]} for command in commands
            ]
        }

    async def toolkit_help(self, command: str | None = None) -> dict[str, Any]:
        """Return the help of one pyGdbToolkit command, or of all of them."""
        commands = await self._toolkit_command_helps()
        if command is None:
            return {"commands": commands}
        selected = [entry for entry in commands if entry["name"] == command.strip()]
        if not selected:
            raise ValueError(f"Unknown pyGdbToolkit command: {command}")
        return {"commands": selected}

    async def _toolkit_command_helps(self) -> list[dict[str, Any]]:
        marker = "PYGDBSERVER_TOOLKIT_HELP_JSON:"
        command = (
            "python import json; from pyGdbToolkit.session import SESSION; "
            f"print('{marker}' + json.dumps([c.to_dict() for c in SESSION.commands]))"
        )
        data = await self._python_json(marker, command)
        if not isinstance(data, list):
            raise RuntimeError("GDB did not return the pyGdbToolkit command help")
        return data

    async def _python_json(self, marker: str, command: str, timeout: float = 30.0) -> Any:
        """Run a GDB Python command and decode the JSON payload printed after ``marker``."""
        result = await self.mi.console(command, timeout)
        for output in result.output:
            for line in output.splitlines():
                payload_index = line.find(marker)
                if payload_index >= 0:
                    return json.loads(line[payload_index + len(marker) :].strip())
        return None

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
        self.rtos_projects.close()
        for process in reversed(tuple(self.core_ocds.values())):
            await process.stop()
        if self.ocd is not None:
            await self.ocd.stop()


def _timeout(params: dict[str, Any]) -> float:
    value = params.get("timeout", 30.0)
    if not isinstance(value, (int, float)) or not 0 < value <= 300:
        raise ValueError("timeout must be between 0 and 300 seconds")
    return float(value)


def _rpc_error(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _free_jlink_ports() -> tuple[int, int, int]:
    """Reserve all three per-core ports simultaneously to prevent internal collisions."""
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as gdb_socket,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as telnet_socket,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as swo_socket,
    ):
        for listener in (gdb_socket, telnet_socket, swo_socket):
            listener.bind(("127.0.0.1", 0))
        return (
            int(gdb_socket.getsockname()[1]),
            int(telnet_socket.getsockname()[1]),
            int(swo_socket.getsockname()[1]),
        )


def _free_loopback_ports() -> tuple[int, int]:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as gdb_socket,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as telnet_socket,
    ):
        gdb_socket.bind(("127.0.0.1", 0))
        telnet_socket.bind(("127.0.0.1", 0))
        return int(gdb_socket.getsockname()[1]), int(telnet_socket.getsockname()[1])


def _toolkit_python_path() -> Path:
    spec = find_spec("pyGdbToolkit")
    if spec is None or spec.origin is None:
        raise RuntimeError("cannot locate the pyGdbToolkit installation")
    return Path(spec.origin).resolve().parent.parent


class RpcMethodNotFound(Exception):
    """Signal a JSON-RPC method lookup failure."""


def _jlink_core_name(messages: list[str]) -> str | None:
    """Parse detected CPU names from a successful J-Link connection, not device settings."""
    names: set[str] = set()
    connected = False
    for message in messages:
        message = message.strip()
        if message == "Connecting to target...":
            names.clear()
            connected = False
        if message.startswith(("ERROR:", "Target connection failed.")) or (
            "Identified core does not match configuration" in message
        ):
            return None
        match = re.fullmatch(r"JTAG ID: 0x[0-9a-fA-F]+ \((Cortex-M\d+(?:\+|P)?)\)", message)
        if match is None:
            match = re.fullmatch(r"Found (Cortex-M\d+(?:\+|P)?)(?: r\d+p\d+)?", message)
        if match is not None:
            names.add(match[1])
        if message == "Connected to target":
            connected = True
    return next(iter(names)) if connected and len(names) == 1 else None


def _ocd_listener_message(executable: str, port: int) -> str | None:
    name = Path(executable).name.lower()
    if "pyocd" in name:
        return f"GDB server listening on port {port}"
    if "openocd" in name:
        return f"Listening on port {port} for gdb connections"
    if "jlinkgdbserver" in name:
        return "Connected to target"
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
