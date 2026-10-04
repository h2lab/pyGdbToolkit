"""Shared session discovery must never promote uncertain or stale identity."""

from dataclasses import replace

from pyGdbToolkit.arch.memmap import MemoryRegion, TargetFingerprint
from pyGdbToolkit.memmap import MemoryMapReport
from pyGdbToolkit.session import ToolkitSession


def test_confirmed_discovery_is_shared_and_invalidated():
    """Publish evidence without altering architecture probing and clear it on switch."""
    session = ToolkitSession()
    context = {"architecture": "armv8.1-m.main", "thread": 1}
    fingerprint = TargetFingerprint(
        "STMicroelectronics",
        "STM32N6",
        context["architecture"],
        "hardware-confirmed",
        ("MCU ROM JEP106 + part",),
    )
    report = MemoryMapReport(
        context["architecture"],
        dict(context),
        [MemoryRegion(0x24000000, 0x24100000, "ram", "gdb-server")],
        fingerprint=fingerprint,
    )
    assert session.publish_discovery(report, lambda: dict(context))
    assert session.target_info is fingerprint
    assert session.memory_regions == tuple(report.regions)
    context["thread"] = 2
    assert session.discovery is None
    assert session.target_info is None
    assert session.memory_regions == ()
    assert report.regions


def test_uncertain_identity_is_not_published():
    """Server-only identity remains report evidence, never confirmed session metadata."""
    session = ToolkitSession()
    context = {"architecture": "arm", "thread": 1}
    fingerprint = TargetFingerprint(
        "STMicroelectronics", "STM32N657", "arm", "server-reported", ("pyOCD configuration",)
    )
    report = MemoryMapReport("arm", context, fingerprint=fingerprint)
    assert not session.publish_discovery(report, lambda: context)
    assert session.target_info is None
    report.fingerprint = replace(fingerprint, confidence="hardware-confirmed")
    assert session.publish_discovery(report, lambda: context)
    session.invalidate()
    assert session.discovery is None
    assert session._probe is None


def test_disconnected_context_is_not_reused():
    """Context failures invalidate shared data instead of leaking another target."""
    session = ToolkitSession()
    context = {"architecture": "arm"}
    fingerprint = TargetFingerprint("ST", "STM32", "arm", "hardware-confirmed", ("hardware",))
    report = MemoryMapReport("arm", context, fingerprint=fingerprint)
    assert session.publish_discovery(report, lambda: context)
    session._discovery_context = lambda: (_ for _ in ()).throw(RuntimeError("disconnected"))
    assert session.discovery is None
