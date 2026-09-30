# SPDX-FileCopyrightText: 2026 H2Lab Development Team
#
# SPDX-License-Identifier: Apache-2.0

"""Session-cached Arm target inspection shared by the Arm-aware GDB commands.

This module is not exported by :mod:`pyGdbToolkit.arch.arm` on purpose: it binds
the Arm inspection results to the unified session, whereas the ``arch`` package
stays independent from any session or GDB concern.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...session import SESSION, SessionSlice, ToolkitSession
from .coresight import CoreSightDiscovery, discover_rom_tables
from .cortex_m import CPUID_ADDRESS, CortexMTargetDescription, decode_cpuid
from .models import DeviceReport
from .providers import DEFAULT_PROVIDER_REGISTRY


@dataclass
class ArmInspectionState(SessionSlice):
    """Cache the Arm identity, the ROM-table discovery, and the vendor device report."""

    target: CortexMTargetDescription | None = None
    discovery: CoreSightDiscovery | None = None
    report: DeviceReport | None = None

    def reset(self) -> None:
        """Drop every cached Arm inspection result."""
        self.target = None
        self.discovery = None
        self.report = None


def cortex_m_target(session: ToolkitSession = SESSION) -> CortexMTargetDescription:
    """Return the decoded Cortex-M CPUID identity of the session target.

    Parameters
    ----------
    session : ToolkitSession
        Session owning the target access and the cached inspection results.

    Returns
    -------
    CortexMTargetDescription
        The decoded CPUID identity.
    """
    state = session.state(ArmInspectionState)
    if state.target is None:
        state.target = decode_cpuid(session.memory.read_uint32(CPUID_ADDRESS))
    return state.target


def rom_table_discovery(session: ToolkitSession = SESSION) -> CoreSightDiscovery:
    """Return the CoreSight ROM-table discovery of the session target.

    Parameters
    ----------
    session : ToolkitSession
        Session owning the target access and the cached inspection results.

    Returns
    -------
    CoreSightDiscovery
        The MCU and processor ROM-table discovery results.
    """
    state = session.state(ArmInspectionState)
    if state.discovery is None:
        state.discovery = discover_rom_tables(session.memory)
    return state.discovery


def device_report(session: ToolkitSession = SESSION) -> DeviceReport:
    """Return the manufacturer device report of the session target.

    Parameters
    ----------
    session : ToolkitSession
        Session owning the target access and the cached inspection results.

    Returns
    -------
    DeviceReport
        The report of the first provider recognizing the target, or a generic one.
    """
    state = session.state(ArmInspectionState)
    if state.report is None:
        state.report = DEFAULT_PROVIDER_REGISTRY.inspect(
            session.memory,
            cortex_m_target(session),
            rom_table_discovery(session),
        )
    return state.report
