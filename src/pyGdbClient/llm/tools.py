# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Fixed tool routes, argument schemas and locally owned effect classifications."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Literal

from .contracts import ActionEffect, JsonObject, JsonValue, ToolCall, ToolDefinition

MAX_MEMORY_READ_BYTES = 4096
MAX_COMMAND_CHARACTERS = 4096


class ToolValidationError(ValueError):
    """An unknown tool or invalid argument was rejected before dispatch."""


@dataclass(frozen=True)
class _Parameter:
    name: str
    kind: Literal["string", "integer", "number"]
    description: str
    required: bool = True
    minimum: int | float = 0
    maximum: int | float = 0
    max_length: int = MAX_COMMAND_CHARACTERS
    default: int | float | None = None

    def schema(self) -> JsonObject:
        result: JsonObject = {"type": self.kind, "description": self.description}
        if self.kind == "string":
            result.update(minLength=1, maxLength=self.max_length, pattern=r"\S")
            result["not"] = {"pattern": r"[\u0000-\u001f\u007f]"}
        else:
            result.update(minimum=self.minimum, maximum=self.maximum)
        if self.default is not None:
            result["default"] = self.default
        return result

    def validate(self, value: JsonValue) -> None:
        valid = False
        if self.kind == "string":
            valid = (
                isinstance(value, str)
                and bool(value.strip())
                and len(value) <= self.max_length
                and all(ord(character) >= 32 and ord(character) != 127 for character in value)
            )
        elif not isinstance(value, bool) and isinstance(value, (int, float)):
            valid = (
                (self.kind == "number" or isinstance(value, int))
                and self.minimum <= value <= self.maximum
                and (not isinstance(value, float) or math.isfinite(value))
            )
        if not valid:
            raise ToolValidationError(f"invalid argument {self.name}: expected {self.schema()}")


_TIMEOUT = _Parameter(
    "timeout", "number", "RPC execution timeout in seconds.", False, 0.001, 300, default=30
)
_COMMAND = _Parameter("command", "string", "One command line; never considered safe by its name.")


@dataclass(frozen=True)
class _Tool:
    name: str
    description: str
    method: str
    effects: tuple[ActionEffect, ...] = (ActionEffect.OBSERVE,)
    parameters: tuple[_Parameter, ...] = ()
    prefix: str = ""

    def definition(self) -> ToolDefinition:
        parameters = (*self.parameters, _TIMEOUT)
        return ToolDefinition(
            self.name,
            self.description,
            {
                "type": "object",
                "properties": {parameter.name: parameter.schema() for parameter in parameters},
                "required": [parameter.name for parameter in parameters if parameter.required],
                "additionalProperties": False,
            },
        )

    def arguments(self, arguments: JsonObject) -> JsonObject:
        if not isinstance(arguments, dict) or not all(isinstance(key, str) for key in arguments):
            raise ToolValidationError("tool arguments must be an object with string keys")
        parameters = (*self.parameters, _TIMEOUT)
        unknown = set(arguments) - {parameter.name for parameter in parameters}
        if unknown:
            raise ToolValidationError(
                f"unknown arguments for {self.name}: {', '.join(sorted(unknown))}"
            )
        normalized: JsonObject = {}
        for parameter in parameters:
            if parameter.name not in arguments:
                if parameter.required:
                    raise ToolValidationError(f"missing argument for {self.name}: {parameter.name}")
                if parameter.default is not None:
                    normalized[parameter.name] = parameter.default
                continue
            value = arguments[parameter.name]
            parameter.validate(value)
            normalized[parameter.name] = value
        return normalized


_UNKNOWN = (ActionEffect.UNKNOWN,)
_TOOLS = (
    _Tool("target_context", "Read the versioned target context without probing.", "target.context"),
    _Tool("target_status", "Read execution state and selected thread/core.", "target.status"),
    _Tool("target_cores", "List debug-server CPU contexts.", "target.cores"),
    _Tool("target_core", "Read the selected CPU context.", "target.core"),
    _Tool(
        "target_select_core",
        "Select a CPU context; attaching may halt it and invalidates subsequent plan context.",
        "target.select_core",
        (ActionEffect.EXECUTION_CONTROL, ActionEffect.UNKNOWN),
        (_Parameter("core", "integer", "Non-negative CPU context ID.", maximum=2**31 - 1),),
    ),
    _Tool(
        "svd_peripherals",
        "Read loaded SVD metadata; no peripheral memory is read.",
        "svd.peripherals",
    ),
    _Tool("toolkit_commands", "List the loaded toolkit commands.", "toolkit.commands"),
    _Tool(
        "toolkit_help",
        "Read toolkit command documentation.",
        "toolkit.help",
        parameters=(
            _Parameter("command", "string", "Optional command name.", False, max_length=128),
        ),
    ),
    _Tool("register_names", "List GDB register names.", "mi.execute"),
    _Tool("register_values", "Read GDB register values in hexadecimal.", "mi.execute"),
    _Tool(
        "memory_read",
        "Read a bounded byte range. MMIO reads may change target state; not a safety guarantee.",
        "mi.execute",
        (ActionEffect.READ_MEMORY,),
        (
            _Parameter(
                "address", "integer", "Start address, as a JSON integer.", maximum=2**64 - 1
            ),
            _Parameter(
                "length",
                "integer",
                "Number of bytes; address + length must not exceed 2**64.",
                minimum=1,
                maximum=MAX_MEMORY_READ_BYTES,
            ),
        ),
    ),
    _Tool(
        "toolkit_command",
        "Run a toolkit/GDB command line. Arbitrary effects, scripts and file access are possible.",
        "command.execute",
        _UNKNOWN,
        (_COMMAND,),
    ),
    _Tool(
        "gdb_command",
        "Run a GDB command line. Arbitrary effects, scripts and file access are possible.",
        "command.execute",
        _UNKNOWN,
        (_COMMAND,),
        "gdb ",
    ),
    _Tool(
        "ocd_command",
        "Run an OCD monitor command (without the monitor prefix). Arbitrary effects are possible.",
        "command.execute",
        _UNKNOWN,
        (_COMMAND,),
        "monitor ",
    ),
    _Tool(
        "mi_command",
        "Run one raw GDB/MI command starting with '-'. Arbitrary effects are possible.",
        "mi.execute",
        _UNKNOWN,
        (_COMMAND,),
    ),
)


@dataclass(frozen=True)
class PreparedCall:
    """Detached invocation data; preparation is validation, never authorization."""

    call_id: str
    name: str
    arguments_json: str
    method: str
    params_json: str
    timeout: float
    effects: tuple[ActionEffect, ...]

    def tool_call(self) -> ToolCall:
        """Return a fresh copy for local policy assessment."""
        return ToolCall(self.call_id, self.name, json.loads(self.arguments_json))


class ToolCatalogue:
    """Advertise schemas and map validated calls to fixed RPC routes."""

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """Return detached tool definitions without executable callbacks or a transport."""
        return tuple(tool.definition() for tool in _TOOLS)

    def prepare(self, call: ToolCall) -> PreparedCall:
        """Validate arguments, apply defaults and freeze the exact RPC invocation."""
        tool = next((tool for tool in _TOOLS if tool.name == call.name), None)
        if tool is None:
            raise ToolValidationError(f"unknown tool: {call.name}")
        arguments = tool.arguments(call.arguments)
        params = dict(arguments)
        if tool.name == "memory_read":
            address, length = arguments["address"], arguments["length"]
            if not isinstance(address, int) or not isinstance(length, int):
                raise ToolValidationError("memory reads require integer address and length")
            if address + length > 2**64:
                raise ToolValidationError("memory range must not exceed 2**64")
            params = {
                "command": f"-data-read-memory-bytes 0x{address:x} {length}",
                "timeout": arguments["timeout"],
            }
        elif tool.name in ("register_names", "register_values"):
            command = (
                "-data-list-register-names"
                if tool.name == "register_names"
                else "-data-list-register-values x"
            )
            params = {"command": command, "timeout": arguments["timeout"]}
        elif "command" in arguments and tool.name != "toolkit_help":
            raw_command = arguments["command"]
            if not isinstance(raw_command, str):
                raise ToolValidationError("command must be a string")
            if tool.name == "mi_command" and not raw_command.startswith("-"):
                raise ToolValidationError("MI command must start with '-'")
            params["command"] = tool.prefix + raw_command
        timeout = arguments["timeout"]
        if not isinstance(timeout, (int, float)):
            raise ToolValidationError("timeout must be a number")
        return PreparedCall(
            call.call_id,
            tool.name,
            json.dumps(arguments, sort_keys=True, allow_nan=False),
            tool.method,
            json.dumps(params, sort_keys=True, allow_nan=False),
            float(timeout),
            tool.effects,
        )
