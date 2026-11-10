# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Non-intrusive snapshot of the target identity already known by the GDB session.

The snapshot only reuses what the session has already established (cached
architecture probe, confirmed memory-map discovery, loaded SVD, selected core)
plus host-side GDB state. It never probes the target to fill a missing field:
every value carries its source, and every missing value carries a reason.
"""

from __future__ import annotations

from typing import Any, Callable

import gdb

from .arch import ProbeResult
from .cmd_svd import SvdSessionState
from .ocd.base import CoreInfo
from .session import SESSION, ToolkitSession

__all__ = ["collect_target_context"]

type Fact = dict[str, str | None]


def _known(value: str, source: str) -> Fact:
    return {"value": value, "source": source, "unavailable_reason": None}


def _unavailable(source: str, reason: str) -> Fact:
    return {"value": None, "source": source, "unavailable_reason": reason}


def _current_core() -> CoreInfo:
    from .core_runtime import CORES

    return CORES.current()


def _gdb_architecture() -> str:
    return str(gdb.selected_inferior().architecture().name())


def collect_target_context(
    session: ToolkitSession = SESSION,
    current_core: Callable[[], CoreInfo] = _current_core,
    gdb_architecture: Callable[[], str] = _gdb_architecture,
) -> dict[str, Any]:
    """Describe the target from session state without new target accesses.

    Parameters
    ----------
    session : ToolkitSession
        Session holding the cached probe, the confirmed discovery and the SVD state.
    current_core : Callable[[], CoreInfo]
        Returns the CPU selected in the debug server.
    gdb_architecture : Callable[[], str]
        Returns the architecture GDB uses for the selected inferior.

    Returns
    -------
    dict[str, Any]
        JSON-serializable facts: ``mcu``, ``svd_device``, ``revision``,
        ``architecture``, ``core``, ``memory_map`` and ``svd_file``.
    """
    probe = session.cached_probe
    target = probe.target if probe is not None else None
    fingerprint = session.target_info
    svd = session.state(SvdSessionState)

    if fingerprint is not None:
        name = fingerprint.soc
        if fingerprint.vendor and fingerprint.vendor.lower() not in name.lower():
            name = f"{fingerprint.vendor} {name}"
        mcu = _known(name, "toolkit-discovery")
    else:
        mcu = _unavailable("toolkit-discovery", "no confirmed identity; run 'memmap discover'")

    svd_device = (
        _known(svd.device.name, "svd")
        if svd.device is not None and svd.device.name
        else _unavailable("svd", "no SVD device loaded")
    )

    if target is not None and target.revision and target.revision.lower() != "unknown":
        revision = _known(target.revision, "architecture-probe")
    else:
        revision = _unavailable("architecture-probe", _probe_reason(probe, "no revision reported"))

    if target is not None:
        architecture = _known(str(target.architecture), "architecture-probe")
    else:
        try:
            architecture = _known(gdb_architecture(), "gdb")
        except (gdb.error, AttributeError, RuntimeError) as error:
            architecture = _unavailable("gdb", f"architecture unavailable: {error}")

    try:
        core_info = current_core()
        core = _known(f"{core_info.name} (id {core_info.id})", core_info.identity_source)
    except (gdb.error, gdb.GdbError, RuntimeError, ValueError) as error:
        core = (
            _known(target.core_name, "architecture-probe")
            if target is not None and target.core_name
            else _unavailable("debug-server", f"selected core unavailable: {error}")
        )

    if session.discovery is not None:
        memory_map: dict[str, Any] = {
            "regions": [
                {
                    "start": region.start,
                    "end": region.end,
                    "kind": region.kind,
                    "source": region.source,
                    "name": region.name,
                    "protection": region.protection,
                }
                for region in session.memory_regions
            ],
            "source": "toolkit-discovery",
            "unavailable_reason": None,
        }
    else:
        memory_map = {
            "regions": [],
            "source": "toolkit-discovery",
            "unavailable_reason": "no confirmed memory map; run 'memmap discover'",
        }

    svd_file = (
        _known(str(svd.svd_path), "svd")
        if svd.svd_path is not None
        else _unavailable("svd", "no SVD file loaded")
    )

    return {
        "mcu": mcu,
        "svd_device": svd_device,
        "revision": revision,
        "architecture": architecture,
        "core": core,
        "memory_map": memory_map,
        "svd_file": svd_file,
    }


def _probe_reason(probe: ProbeResult | None, default: str) -> str:
    if probe is None:
        return "target not identified yet; run 'lscpu'"
    if probe.unavailable_reason:
        return probe.unavailable_reason
    return default
