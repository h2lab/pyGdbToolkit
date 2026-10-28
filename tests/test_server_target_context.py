# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""The ``target.context`` RPC merges GDB facts and configuration into a versioned snapshot."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import pytest

from pyGdbServer.config import ServerConfig, load_config
from pyGdbServer.mi import MiResult
from pyGdbServer.server import PyGdbServer
from pyGdbServer.target_context import build_target_context, debug_transport, ocd_identity

MARKER = "PYGDBSERVER_TARGET_CONTEXT_JSON:"

GDB_FACTS: dict[str, Any] = {
    "mcu": {
        "value": "Raspberry Pi RP2350",
        "source": "toolkit-discovery",
        "unavailable_reason": None,
    },
    "svd_device": {"value": "RP2350", "source": "svd", "unavailable_reason": None},
    "revision": {"value": "r1p0", "source": "architecture-probe", "unavailable_reason": None},
    "architecture": {"value": "arm", "source": "architecture-probe", "unavailable_reason": None},
    "core": {"value": "rp2350.cm0 (id 0)", "source": "debug-server", "unavailable_reason": None},
    "memory_map": {
        "regions": [{"start": 0, "end": 4096, "kind": "rom", "source": "gdb-server"}],
        "source": "toolkit-discovery",
        "unavailable_reason": None,
    },
    "svd_file": {"value": "/srv/rp2350.svd", "source": "svd", "unavailable_reason": None},
}


def _config(**changes: Any) -> ServerConfig:
    config = ServerConfig(
        gdb_path="gdb-multiarch",
        gdb_args=(),
        ocd_path="/usr/bin/openocd",
        ocd_args=("-f", "target/rp2350.cfg"),
        listen_host="localhost",
        listen_port=0,
        gdb_init=(),
        log_directory=Path("logs"),
        startup_timeout=1.0,
        ocd_identifier="openocd",
    )
    return replace(config, **changes)


class ContextMiSession:
    """Answer the context collection command with a scripted GDB output."""

    def __init__(self, output: tuple[str, ...] = (), error: Exception | None = None) -> None:
        """Store the scripted output or failure."""
        self.output = output
        self.error = error
        self.calls: list[tuple[str, float]] = []

    async def console(self, command: str, timeout: float = 30.0) -> MiResult:
        """Record the command and replay the scripted answer."""
        self.calls.append((command, timeout))
        if self.error is not None:
            raise self.error
        return MiResult("done", "done", self.output)


def _rpc(server: PyGdbServer, params: dict[str, Any]) -> dict[str, Any]:
    response, _ = asyncio.run(
        server.handle_rpc_message(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "target.context", "params": params})
        )
    )
    assert response is not None
    return response


def _server(mi: ContextMiSession, config: ServerConfig | None = None) -> PyGdbServer:
    server = object.__new__(PyGdbServer)
    server.config = config or _config()
    server.mi = mi  # type: ignore[assignment]
    return server


def test_rpc_returns_versioned_snapshot_from_gdb_facts() -> None:
    """The toolkit collector runs inside GDB and its facts are passed through."""
    mi = ContextMiSession((MARKER + json.dumps(GDB_FACTS),))

    response = _rpc(_server(mi, _config(debug_transport="swd")), {"timeout": 5})

    result = response["result"]
    assert result["schema_version"] == 1
    assert result["context_id"].startswith("ctx-")
    for name in ("mcu", "revision", "architecture", "core", "svd_file"):
        assert result[name] == GDB_FACTS[name]
    assert result["memory_map"] == GDB_FACTS["memory_map"]
    assert result["debug_transport"] == {
        "value": "SWD",
        "source": "configuration",
        "unavailable_reason": None,
    }
    assert result["ocd"]["value"] == "openocd (openocd)"
    assert "svd_device" not in result
    assert "collect_target_context()" in mi.calls[0][0]
    assert mi.calls[0][1] == 5


@pytest.mark.parametrize(
    "mi",
    [ContextMiSession(("no marker",)), ContextMiSession(error=RuntimeError("GDB is not running"))],
)
def test_gdb_failure_still_returns_explained_snapshot(mi: ContextMiSession) -> None:
    """Configured metadata survives a GDB failure; GDB facts explain why they are missing."""
    response = _rpc(_server(mi, _config(target_mcu="RP2350", target_revision="A2")), {})

    result = response["result"]
    assert result["mcu"] == {
        "value": "RP2350",
        "source": "configuration",
        "unavailable_reason": None,
    }
    assert result["revision"]["value"] == "A2"
    for name in ("architecture", "core", "svd_file"):
        assert result[name]["value"] is None
        assert result[name]["unavailable_reason"]
    assert result["memory_map"]["regions"] == []
    assert result["memory_map"]["unavailable_reason"]


def test_mcu_precedence_prefers_confirmed_hardware_then_configuration_then_svd() -> None:
    """Configuration does not override a hardware-confirmed identity; SVD is the last hint."""
    unconfirmed = dict(
        GDB_FACTS,
        mcu={"value": None, "source": "toolkit-discovery", "unavailable_reason": "not run"},
    )
    assert build_target_context(_config(target_mcu="X"), GDB_FACTS)["mcu"] == GDB_FACTS["mcu"]
    assert build_target_context(_config(target_mcu="X"), unconfirmed)["mcu"]["value"] == "X"
    assert build_target_context(_config(), unconfirmed)["mcu"] == GDB_FACTS["svd_device"]
    no_svd = dict(
        unconfirmed, svd_device={"value": None, "source": "svd", "unavailable_reason": "none"}
    )
    assert build_target_context(_config(), no_svd)["mcu"] == unconfirmed["mcu"]


def test_malformed_gdb_facts_are_reported_as_unavailable() -> None:
    """Facts without provenance or with contradictory states are not trusted."""
    facts = dict(
        GDB_FACTS,
        core={"value": "cm0", "source": ""},
        architecture={"value": "arm", "source": "gdb", "unavailable_reason": "contradiction"},
        svd_file="raw",
        memory_map={"regions": "invalid"},
    )

    result = build_target_context(_config(), facts)

    assert result["core"]["unavailable_reason"] == "GDB returned a fact without provenance"
    assert result["architecture"]["unavailable_reason"] == "GDB returned a malformed fact"
    assert result["svd_file"]["value"] is None
    assert result["memory_map"]["regions"] == []
    assert result["memory_map"]["unavailable_reason"]


def test_context_id_changes_with_core_svd_or_memory_map() -> None:
    """Any change of the inputs identifies a new snapshot; identical inputs do not."""
    config = _config()
    reference = build_target_context(config, GDB_FACTS)["context_id"]
    assert build_target_context(config, json.loads(json.dumps(GDB_FACTS)))["context_id"] == (
        reference
    )
    changes = (
        {"core": {"value": "rp2350.cm1 (id 1)", "source": "debug-server"}},
        {"svd_file": {"value": "/srv/other.svd", "source": "svd"}},
        {"memory_map": dict(GDB_FACTS["memory_map"], regions=[])},
    )
    for change in changes:
        assert build_target_context(config, dict(GDB_FACTS, **change))["context_id"] != reference
    assert build_target_context(_config(debug_transport="jtag"), GDB_FACTS)["context_id"] != (
        reference
    )


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"debug_transport": "jtag"}, ("JTAG", "configuration")),
        (
            {
                "ocd_identifier": "jlinkgdbserver",
                "ocd_path": "JLinkGDBServerCLExe",
                "ocd_args": ("-device", "RP2350_M33_0", "-if", "swd"),
            },
            ("SWD", "ocd-args"),
        ),
        ({"ocd_args": ("-c", "transport select jtag", "-f", "t.cfg")}, ("JTAG", "ocd-args")),
        ({"ocd_args": ("-c", "transport select dapdirect_swd")}, (None, "configuration")),
        ({"ocd_identifier": "pyocd", "ocd_args": ("-if", "jtag")}, (None, "configuration")),
    ],
)
def test_debug_transport_is_declared_or_explicit_in_ocd_arguments(
    changes: dict[str, Any], expected: tuple[str | None, str]
) -> None:
    """Only unambiguous declarations are reported; anything else is unknown."""
    fact = debug_transport(_config(**changes))
    assert (fact["value"], fact["source"]) == expected
    assert (fact["unavailable_reason"] is None) == (expected[0] is not None)


def test_ocd_identity_uses_declared_or_inferred_backend() -> None:
    """The OCD fact names the backend and the executable actually launched."""
    assert ocd_identity(_config())["value"] == "openocd (openocd)"
    inferred = ocd_identity(_config(ocd_identifier="", ocd_path="/opt/pyocd"))
    assert (inferred["value"], inferred["source"]) == ("pyocd (pyocd)", "ocd-path")
    unknown = ocd_identity(_config(ocd_identifier="", ocd_path="/opt/wrapper"))
    assert (unknown["value"], unknown["source"]) == ("wrapper", "ocd-path")


def _write(path: Path, **extra: object) -> Path:
    data: dict[str, object] = {"gdb-path": "gdb", "ocd-path": "pyocd", **extra}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_config_accepts_optional_target_metadata(tmp_path: Path) -> None:
    """Declared target metadata is normalized; it is optional."""
    config = load_config(
        _write(
            tmp_path / "c.json",
            target={"mcu": " RP2350 ", "revision": "A2", "debug-transport": "SWD"},
        )
    )
    assert (config.target_mcu, config.target_revision, config.debug_transport) == (
        "RP2350",
        "A2",
        "swd",
    )
    default = load_config(_write(tmp_path / "d.json"))
    assert (default.target_mcu, default.target_revision, default.debug_transport) == ("", "", "")


@pytest.mark.parametrize(
    "target",
    [
        [],
        {"mcu": ""},
        {"revision": 2},
        {"debug-transport": "spi"},
        {"vendor": "ST"},
    ],
)
def test_config_rejects_invalid_target_metadata(tmp_path: Path, target: object) -> None:
    """Invalid declarations fail at load time rather than misinforming the agent."""
    with pytest.raises(ValueError, match="target"):
        load_config(_write(tmp_path / "c.json", target=target))
