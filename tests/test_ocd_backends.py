# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Contract, registry and compatibility tests for the OCD package hierarchy."""

from __future__ import annotations

from importlib.util import find_spec

import pytest

from pyGdbToolkit import core_runtime, ocd
from pyGdbToolkit.ocd.base import CoreBackend, CoreInfo, CoreSessionState
from pyGdbToolkit.ocd.context import GdbCoreContext
from pyGdbToolkit.ocd.detection import OcdDetector, OcdIdentifier
from pyGdbToolkit.ocd.jlinkgdbserver import JLinkClusterState, JLinkCoreBackend, JLinkCoreState
from pyGdbToolkit.ocd.openocd import OpenOcdCoreBackend
from pyGdbToolkit.ocd.pyocd import PyOcdCoreBackend
from pyGdbToolkit.ocd.registry import CoreBackendRegistry
from pyGdbToolkit.session import ToolkitSession


@pytest.mark.parametrize(
    "identifier,implementation",
    (
        (OcdIdentifier.JLINK, JLinkCoreBackend),
        (OcdIdentifier.OPENOCD, OpenOcdCoreBackend),
        (OcdIdentifier.PYOCD, PyOcdCoreBackend),
    ),
)
def test_registered_backends_share_the_same_contract(
    identifier: OcdIdentifier, implementation: type[CoreBackend]
) -> None:
    """All server strategies implement the same list/current/select interface."""
    context = GdbCoreContext(ToolkitSession())
    registry = CoreBackendRegistry(context)
    backend = registry.resolve(identifier)

    assert isinstance(backend, implementation)
    assert isinstance(backend, CoreBackend)
    assert backend.context is context
    assert registry.resolve(identifier) is backend
    assert callable(backend.list_cores)
    assert callable(backend.current_core)
    assert callable(backend.select_core)


def test_unknown_backend_never_falls_back_to_openocd() -> None:
    """An unknown identity is not interpreted as a different server protocol."""
    import gdb

    registry = CoreBackendRegistry(GdbCoreContext(ToolkitSession()))
    with pytest.raises(gdb.GdbError, match="recognized debug server"):
        registry.resolve(OcdIdentifier.UNKNOWN)


def test_detection_public_api_and_core_state_aliases_remain_compatible() -> None:
    """Existing imports refer to the new owning modules without duplicate state classes."""
    assert ocd.OcdDetector is OcdDetector
    assert ocd.OcdIdentifier is OcdIdentifier
    assert core_runtime.CoreInfo is CoreInfo
    assert core_runtime.CoreSessionState is CoreSessionState
    assert core_runtime.JLinkClusterState is JLinkClusterState
    assert core_runtime.JLinkCoreState is JLinkCoreState
    assert find_spec("pyGdbToolkit.core_jlink") is None
    for module in (
        "base",
        "context",
        "detection",
        "registry",
        "jlinkgdbserver",
        "openocd",
        "pyocd",
    ):
        assert find_spec(f"pyGdbToolkit.ocd.{module}") is not None


@pytest.mark.parametrize("identifier", (OcdIdentifier.OPENOCD, OcdIdentifier.PYOCD))
def test_registration_is_an_explicit_backend_capability(identifier: OcdIdentifier) -> None:
    """Backends with server inventories reject configured J-Link registration."""
    import gdb

    backend = CoreBackendRegistry(GdbCoreContext(ToolkitSession())).resolve(identifier)
    with pytest.raises(gdb.GdbError, match="registration requires J-Link"):
        backend.register_cluster({0: "127.0.0.1:4000"}, 0)
    with pytest.raises(gdb.GdbError, match="registration requires J-Link"):
        backend.register_attached_core("Cortex-M7")
