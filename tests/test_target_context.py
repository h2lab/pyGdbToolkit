# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""The GDB-side target context reuses session knowledge and never probes the target."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import gdb
import pytest

from pyGdbToolkit.arch import Architecture, ProbeResult, TargetDescription
from pyGdbToolkit.arch.memmap import MemoryRegion, TargetFingerprint
from pyGdbToolkit.cmd_svd import SvdSessionState
from pyGdbToolkit.memmap import MemoryMapReport
from pyGdbToolkit.ocd.base import CoreInfo
from pyGdbToolkit.session import ToolkitSession
from pyGdbToolkit.target_context import collect_target_context


class NoProbeSession(ToolkitSession):
    """Fail the test if collecting the context triggers a target probe."""

    def probe(self) -> ProbeResult:
        """Reject any new identification."""
        raise AssertionError("collecting the target context must not probe the target")


def _core() -> CoreInfo:
    return CoreInfo(1, "rp2350.cm1", True, "localhost:3333")


def _failing_core() -> CoreInfo:
    raise gdb.GdbError("no debug server")


def test_empty_session_explains_every_missing_fact() -> None:
    """Nothing is guessed: each unknown fact has a source and an actionable reason."""
    session = NoProbeSession()

    context = collect_target_context(session, _failing_core, lambda: "armv8-m.main")

    assert context["architecture"] == {
        "value": "armv8-m.main",
        "source": "gdb",
        "unavailable_reason": None,
    }
    for name in ("mcu", "svd_device", "revision", "core", "svd_file"):
        assert context[name]["value"] is None
        assert context[name]["unavailable_reason"]
    assert "lscpu" in context["revision"]["unavailable_reason"]
    assert context["memory_map"]["regions"] == []
    assert "memmap discover" in context["memory_map"]["unavailable_reason"]
    json.dumps(context)


def test_established_session_state_is_reported_with_provenance(tmp_path: Path) -> None:
    """Probe, confirmed discovery, SVD and core are reused as they are."""
    session = NoProbeSession()
    session._probe = ProbeResult.detected(
        TargetDescription(Architecture.ARM, "Cortex-M", "Cortex-M33", "r1p0")
    )
    discovery_context = {"architecture": "arm", "thread": 1}
    report = MemoryMapReport(
        "arm",
        dict(discovery_context),
        [MemoryRegion(0x20000000, 0x20082000, "ram", "gdb-server", "SRAM", True, "rw")],
        fingerprint=TargetFingerprint(
            "Raspberry Pi", "RP2350", "arm", "hardware-confirmed", ("ROM table",)
        ),
    )
    assert session.publish_discovery(report, lambda: dict(discovery_context))
    svd = session.state(SvdSessionState)
    svd.device = SimpleNamespace(name="RP2350")  # type: ignore[assignment]
    svd.svd_path = tmp_path / "rp2350.svd"

    context = collect_target_context(session, _core, lambda: pytest.fail("GDB not needed"))

    assert context["mcu"]["value"] == "Raspberry Pi RP2350"
    assert context["mcu"]["source"] == "toolkit-discovery"
    assert context["svd_device"]["value"] == "RP2350"
    assert context["revision"] == {
        "value": "r1p0",
        "source": "architecture-probe",
        "unavailable_reason": None,
    }
    assert context["architecture"]["value"] == "arm"
    assert context["core"] == {
        "value": "rp2350.cm1 (id 1)",
        "source": "debug-server",
        "unavailable_reason": None,
    }
    assert context["memory_map"] == {
        "regions": [
            {
                "start": 0x20000000,
                "end": 0x20082000,
                "kind": "ram",
                "source": "gdb-server",
                "name": "SRAM",
                "protection": "rw",
            }
        ],
        "source": "toolkit-discovery",
        "unavailable_reason": None,
    }
    assert context["svd_file"]["value"] == str(tmp_path / "rp2350.svd")
    json.dumps(context)


def test_unknown_revision_and_unreachable_core_fall_back_explicitly() -> None:
    """A placeholder revision is not a fact; the probed core name replaces a lost server."""
    session = NoProbeSession()
    session._probe = ProbeResult.detected(
        TargetDescription(Architecture.RISCV, "RISC-V", "RV32IM", "unknown")
    )

    context = collect_target_context(session, _failing_core, lambda: "unused")

    assert context["revision"]["value"] is None
    assert context["revision"]["unavailable_reason"] == "no revision reported"
    assert context["core"]["value"] == "RV32IM"
    assert context["core"]["source"] == "architecture-probe"


def test_failed_probe_reason_is_preserved() -> None:
    """An earlier unsuccessful identification explains the missing revision."""
    session = NoProbeSession()
    session._probe = ProbeResult.unavailable("CPUID unreadable", access_error=True)

    def no_architecture() -> str:
        raise gdb.error("no inferior")

    context = collect_target_context(session, _core, no_architecture)

    assert context["revision"]["unavailable_reason"] == "CPUID unreadable"
    assert context["architecture"]["value"] is None
    assert "no inferior" in context["architecture"]["unavailable_reason"]
