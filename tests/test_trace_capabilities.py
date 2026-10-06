"""Portable trace dispatch, session caching, and invalidation tests."""

import pytest

from pyGdbToolkit.arch import Architecture, ProbeResult, TargetDescription
from pyGdbToolkit.arch.trace import (
    TraceCapabilities,
    TraceCapabilityRegistry,
    TraceComponent,
    TraceComponentKind,
)
from pyGdbToolkit.session import ToolkitSession


@pytest.fixture
def no_memory_reads(monkeypatch):
    """Provide a reader that rejects any architecture-specific access."""

    class Reader:
        def read_uint32(self, address):
            raise AssertionError(f"unexpected read at {address:#x}")

    monkeypatch.setattr("pyGdbToolkit.session.TargetMemoryReader", Reader)


def test_session_caches_and_invalidates_trace_capabilities(no_memory_reads):
    """Capability dispatch works for non-Arm backends without session changes."""
    calls = []
    target = TargetDescription(Architecture.RISCV, "riscv", "test", "r0")
    result = TraceCapabilities((TraceComponent(TraceComponentKind.MTB, 0x1000),))

    def inspect(reader, description):
        calls.append(description)
        return result

    session = ToolkitSession(trace_registry=TraceCapabilityRegistry({Architecture.RISCV: inspect}))
    session._probe = ProbeResult.detected(target)
    assert session.has_mtb()
    assert not session.has_etm()
    assert not session.has_etb()
    assert not session.has_etf()
    assert session.trace_capabilities() is result
    assert calls == [target]
    session.invalidate()
    assert session._trace_capabilities is None
    session._probe = ProbeResult.detected(target)
    assert session.trace_capabilities() is result
    assert calls == [target, target]
    session.reset()
    assert session._trace_capabilities is None


def test_unsupported_architecture_does_not_probe_arm_addresses(no_memory_reads):
    """An unsupported backend is unavailable, not evidence of absent hardware."""
    session = ToolkitSession()
    session._probe = ProbeResult.detected(
        TargetDescription(Architecture.XTENSA, "test", "test", "r0")
    )
    result = session.trace_capabilities()
    assert not result.is_available
    assert "xtensa" in result.unavailable_reason
    assert not session.has_etm()


def test_unidentified_target_retains_probe_failure():
    """Failure to identify the processor is preserved by the capability API."""
    session = ToolkitSession()
    session._probe = ProbeResult.unavailable("target disconnected")
    result = session.trace_capabilities()
    assert result.unavailable_reason == "target disconnected"
    assert not session.has_etb()
