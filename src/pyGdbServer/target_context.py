# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Versioned target-context snapshot combining GDB-side facts and server configuration.

Each fact is ``{"value", "source", "unavailable_reason"}``: a value with its
provenance, or no value with the reason it is unknown. The snapshot identifier
is derived from the content, so it changes whenever the target, the selected
core, the SVD file or the memory map changes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .config import ServerConfig

SCHEMA_VERSION = 1

type Fact = dict[str, str | None]


def _known(value: str, source: str) -> Fact:
    return {"value": value, "source": source, "unavailable_reason": None}


def _unavailable(source: str, reason: str) -> Fact:
    return {"value": None, "source": source, "unavailable_reason": reason}


def _fact(raw: object, source: str, reason: str) -> Fact:
    """Accept a well-formed GDB-side fact, else report it as unavailable."""
    if not isinstance(raw, dict):
        return _unavailable(source, reason)
    value, raw_source, unavailable = (
        raw.get("value"),
        raw.get("source"),
        raw.get("unavailable_reason"),
    )
    if not isinstance(raw_source, str) or not raw_source.strip():
        return _unavailable(source, "GDB returned a fact without provenance")
    if isinstance(value, str) and value.strip() and unavailable is None:
        return _known(value, raw_source)
    if value is None and isinstance(unavailable, str) and unavailable.strip():
        return _unavailable(raw_source, unavailable)
    return _unavailable(source, "GDB returned a malformed fact")


def _first_known(*facts: Fact) -> Fact:
    """Return the first available fact, else the first explanation."""
    return next((fact for fact in facts if fact["value"] is not None), facts[0])


def debug_transport(config: ServerConfig) -> Fact:
    """Report the declared debug transport, or the one explicit in the OCD arguments."""
    if config.debug_transport:
        return _known(config.debug_transport.upper(), "configuration")
    arguments = config.ocd_args
    backend = config.backend_identifier
    if backend == "jlinkgdbserver":
        for flag, value in zip(arguments, arguments[1:]):
            if flag.lower() == "-if" and value.lower() in ("jtag", "swd"):
                return _known(value.upper(), "ocd-args")
    elif backend == "openocd":
        for flag, value in zip(arguments, arguments[1:]):
            words = value.lower().split()
            if flag == "-c" and words[:2] == ["transport", "select"] and len(words) == 3:
                if words[2] in ("jtag", "swd"):
                    return _known(words[2].upper(), "ocd-args")
    return _unavailable(
        "configuration",
        "transport not declared; set target.debug-transport in the server configuration",
    )


def ocd_identity(config: ServerConfig) -> Fact:
    """Name the debug server driving the probe."""
    executable = Path(config.ocd_path).name
    if config.ocd_identifier:
        return _known(f"{config.ocd_identifier} ({executable})", "configuration")
    if config.backend_identifier:
        return _known(f"{config.backend_identifier} ({executable})", "ocd-path")
    return _known(executable, "ocd-path")


def build_target_context(
    config: ServerConfig, gdb_facts: object, gdb_failure: str | None = None
) -> dict[str, Any]:
    """Merge GDB-side facts with configured metadata into a versioned snapshot.

    Parameters
    ----------
    config : ServerConfig
        Server configuration, including optional declared target metadata.
    gdb_facts : object
        Payload returned by ``pyGdbToolkit.target_context.collect_target_context``.
    gdb_failure : str | None
        Reason the GDB-side collection failed, if it did.

    Returns
    -------
    dict[str, Any]
        JSON snapshot with ``schema_version``, ``context_id``, the facts and the memory map.
    """
    reason = gdb_failure or "GDB did not return the toolkit target context"
    raw = gdb_facts if isinstance(gdb_facts, dict) else {}

    def gdb_fact(name: str, source: str) -> Fact:
        return _fact(raw.get(name), source, reason)

    configured_mcu = (
        _known(config.target_mcu, "configuration")
        if config.target_mcu
        else _unavailable("configuration", "target.mcu not declared")
    )
    configured_revision = (
        _known(config.target_revision, "configuration")
        if config.target_revision
        else _unavailable("configuration", "target.revision not declared")
    )
    context: dict[str, Any] = {
        "mcu": _first_known(
            gdb_fact("mcu", "toolkit-discovery"), configured_mcu, gdb_fact("svd_device", "svd")
        ),
        "revision": _first_known(gdb_fact("revision", "architecture-probe"), configured_revision),
        "architecture": gdb_fact("architecture", "gdb"),
        "debug_transport": debug_transport(config),
        "ocd": ocd_identity(config),
        "core": gdb_fact("core", "debug-server"),
        "memory_map": _memory_map(raw.get("memory_map"), reason),
        "svd_file": gdb_fact("svd_file", "svd"),
    }
    digest = hashlib.sha256(
        json.dumps(context, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {"schema_version": SCHEMA_VERSION, "context_id": f"ctx-{digest[:16]}", **context}


def _memory_map(raw: object, reason: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("regions"), list):
        return {"regions": [], "source": "toolkit-discovery", "unavailable_reason": reason}
    unavailable = raw.get("unavailable_reason")
    if not isinstance(unavailable, str) or not unavailable.strip():
        unavailable = None
    source = raw.get("source")
    return {
        "regions": [] if unavailable else list(raw["regions"]),
        "source": source if isinstance(source, str) and source.strip() else "toolkit-discovery",
        "unavailable_reason": unavailable,
    }
