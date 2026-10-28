# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Tool schemas and strict validation share locally owned, fixed RPC routes."""

import json
import re
from typing import Any

import pytest

from pyGdbClient.llm.contracts import ActionEffect, ToolCall
from pyGdbClient.llm.tools import (
    MAX_COMMAND_CHARACTERS,
    MAX_MEMORY_READ_BYTES,
    ToolCatalogue,
    ToolValidationError,
)


@pytest.mark.parametrize(
    "name,arguments,method,params,effects",
    [
        ("target_context", {}, "target.context", {}, (ActionEffect.OBSERVE,)),
        ("target_status", {}, "target.status", {}, (ActionEffect.OBSERVE,)),
        ("target_cores", {}, "target.cores", {}, (ActionEffect.OBSERVE,)),
        ("target_core", {}, "target.core", {}, (ActionEffect.OBSERVE,)),
        (
            "target_select_core",
            {"core": 1},
            "target.select_core",
            {"core": 1},
            (ActionEffect.EXECUTION_CONTROL, ActionEffect.UNKNOWN),
        ),
        ("svd_peripherals", {}, "svd.peripherals", {}, (ActionEffect.OBSERVE,)),
        ("toolkit_commands", {}, "toolkit.commands", {}, (ActionEffect.OBSERVE,)),
        ("toolkit_help", {}, "toolkit.help", {}, (ActionEffect.OBSERVE,)),
        (
            "toolkit_help",
            {"command": "memmap"},
            "toolkit.help",
            {"command": "memmap"},
            (ActionEffect.OBSERVE,),
        ),
        (
            "register_names",
            {},
            "mi.execute",
            {"command": "-data-list-register-names"},
            (ActionEffect.OBSERVE,),
        ),
        (
            "register_values",
            {},
            "mi.execute",
            {"command": "-data-list-register-values x"},
            (ActionEffect.OBSERVE,),
        ),
        (
            "memory_read",
            {"address": 0x20000000, "length": 16},
            "mi.execute",
            {"command": "-data-read-memory-bytes 0x20000000 16"},
            (ActionEffect.READ_MEMORY,),
        ),
        (
            "toolkit_command",
            {"command": "svd list"},
            "command.execute",
            {"command": "svd list"},
            (ActionEffect.UNKNOWN,),
        ),
        (
            "gdb_command",
            {"command": "info registers"},
            "command.execute",
            {"command": "gdb info registers"},
            (ActionEffect.UNKNOWN,),
        ),
        (
            "ocd_command",
            {"command": "reset halt"},
            "command.execute",
            {"command": "monitor reset halt"},
            (ActionEffect.UNKNOWN,),
        ),
        (
            "mi_command",
            {"command": "-exec-continue"},
            "mi.execute",
            {"command": "-exec-continue"},
            (ActionEffect.UNKNOWN,),
        ),
    ],
)
def test_routes_and_effects_are_owned_by_the_catalogue(
    name: str,
    arguments: dict[str, Any],
    method: str,
    params: dict[str, Any],
    effects: tuple[ActionEffect, ...],
) -> None:
    """The model supplies arguments, never RPC methods, prefixes or effect classifications."""
    prepared = ToolCatalogue().prepare(ToolCall("call-1", name, arguments))
    assert prepared.call_id == "call-1"
    assert prepared.method == method
    assert prepared.effects == effects
    assert prepared.timeout == 30
    assert json.loads(prepared.params_json) == dict(params, timeout=30)
    assert prepared.tool_call().arguments == dict(arguments, timeout=30)


def test_definitions_and_prepared_calls_are_detached() -> None:
    """Mutating advertised schemas or proposed arguments cannot alter trusted routes."""
    catalogue = ToolCatalogue()
    definitions = catalogue.definitions()
    assert len(definitions) == 15
    assert len({definition.name for definition in definitions}) == len(definitions)
    assert all(not hasattr(definition, "handler") for definition in definitions)
    definitions[0].input_schema.clear()
    assert catalogue.definitions()[0].input_schema["type"] == "object"
    call = ToolCall("read", "memory_read", {"address": 0, "length": 4})
    prepared = catalogue.prepare(call)
    call.arguments["address"] = 100
    prepared.tool_call().arguments["length"] = 100
    assert json.loads(prepared.params_json)["command"] == "-data-read-memory-bytes 0x0 4"
    assert prepared.tool_call().arguments == {"address": 0, "length": 4, "timeout": 30}


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("server.shutdown", {}),
        ("workspace.upload", {"filename": "x"}),
        ("target_context", {"method": "server.shutdown"}),
        ("target_context", {"effects": ["observe"]}),
        ("target_context", {1: "not a string key"}),
        ("target_context", []),
        ("target_context", {"timeout": True}),
        ("target_context", {"timeout": 0}),
        ("target_context", {"timeout": 300.001}),
        ("target_context", {"timeout": float("nan")}),
        ("target_context", {"timeout": float("inf")}),
        ("target_context", {"timeout": "30"}),
        ("target_select_core", {}),
        ("target_select_core", {"core": -1}),
        ("target_select_core", {"core": True}),
        ("target_select_core", {"core": 1.0}),
        ("memory_read", {"address": "0x20000000", "length": 4}),
        ("memory_read", {"address": -1, "length": 4}),
        ("memory_read", {"address": True, "length": 4}),
        ("memory_read", {"address": 0, "length": 0}),
        ("memory_read", {"address": 0, "length": MAX_MEMORY_READ_BYTES + 1}),
        ("memory_read", {"address": 0, "length": 1.0}),
        ("memory_read", {"address": 2**64 - 1, "length": 2}),
        ("memory_read", {"address": 2**64, "length": 1}),
        ("memory_read", {"address": 0, "length": 1, "command": "-exec-continue"}),
        ("gdb_command", {"command": ""}),
        ("gdb_command", {"command": " "}),
        ("gdb_command", {"command": "info registers\ncontinue"}),
        ("gdb_command", {"command": "info registers\n"}),
        ("ocd_command", {"command": "reset\rhalt"}),
        ("toolkit_command", {"command": "svd\0write"}),
        ("toolkit_command", {"command": "svd\x7flist"}),
        ("mi_command", {"command": "info registers"}),
        ("mi_command", {"command": "-exec-continue\n-exec-abort"}),
        ("toolkit_help", {"command": None}),
        ("toolkit_help", {"command": "x" * 129}),
        ("gdb_command", {"command": "x" * (MAX_COMMAND_CHARACTERS + 1)}),
    ],
)
def test_invalid_calls_are_rejected(name: str, arguments: Any) -> None:
    """Unknown tools, injected fields, invalid types and out-of-bounds inputs fail early."""
    with pytest.raises(ToolValidationError):
        ToolCatalogue().prepare(ToolCall("call-1", name, arguments))


def test_exact_numeric_and_command_boundaries() -> None:
    """Maximum read size, final 64-bit byte, command size and timeout boundaries are accepted."""
    catalogue = ToolCatalogue()
    for address, length in ((0, MAX_MEMORY_READ_BYTES), (2**64 - 1, 1)):
        prepared = catalogue.prepare(
            ToolCall("r", "memory_read", {"address": address, "length": length})
        )
        assert json.loads(prepared.params_json)["command"] == (
            f"-data-read-memory-bytes 0x{address:x} {length}"
        )
    for timeout in (0.001, 300):
        prepared = catalogue.prepare(
            ToolCall(
                "c", "gdb_command", {"command": "x" * MAX_COMMAND_CHARACTERS, "timeout": timeout}
            )
        )
        assert prepared.timeout == timeout
        assert json.loads(prepared.params_json)["timeout"] == timeout


def test_schema_describes_bounds_and_rejects_control_characters() -> None:
    """Advertised schemas contain the same primitive bounds and additional-field prohibition."""
    schemas = {
        definition.name: definition.input_schema for definition in ToolCatalogue().definitions()
    }
    schema = schemas["memory_read"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["address", "length"]
    properties: Any = schema["properties"]
    assert properties["length"]["maximum"] == MAX_MEMORY_READ_BYTES
    assert properties["address"]["maximum"] == 2**64 - 1
    assert properties["timeout"]["minimum"] == 0.001
    assert properties["timeout"]["maximum"] == 300
    properties = schemas["gdb_command"]["properties"]
    assert properties["command"]["maxLength"] == MAX_COMMAND_CHARACTERS
    for command in ("info registers", "info registers\n", "a\0b", "  "):
        advertised_valid = (
            re.search(properties["command"]["pattern"], command) is not None
            and re.search(properties["command"]["not"]["pattern"], command) is None
        )
        try:
            ToolCatalogue().prepare(ToolCall("c", "gdb_command", {"command": command}))
        except ToolValidationError:
            assert not advertised_valid
        else:
            assert advertised_valid
