# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Manufacturer-agnostic Arm device-provider contracts and dispatch."""

from __future__ import annotations

from typing import Protocol

from ..coresight import CoreSightDiscovery
from ..cortex_m import CortexMTargetDescription
from ..models import DeviceReport, FieldValue
from ....target_memory import TargetMemory


class DeviceProvider(Protocol):
    """A manufacturer-specific device recognizer."""

    def inspect(
        self,
        reader: TargetMemory,
        target: CortexMTargetDescription,
        discovery: CoreSightDiscovery,
    ) -> DeviceReport | None:
        """Return a report if the provider recognizes the target."""
        ...


class ProviderRegistry:
    """Apply device providers in a defined order with a generic fallback."""

    def __init__(self, providers: tuple[DeviceProvider, ...]) -> None:
        """Create a registry from providers evaluated in tuple order."""
        self._providers = providers

    def inspect(
        self,
        reader: TargetMemory,
        target: CortexMTargetDescription,
        discovery: CoreSightDiscovery,
    ) -> DeviceReport:
        """Return the first matching provider report or a generic fallback."""
        for provider in self._providers:
            report = provider.inspect(reader, target, discovery)
            if report is not None:
                return report
        return _generic_report(target, discovery)


def _generic_report(
    target: CortexMTargetDescription,
    discovery: CoreSightDiscovery,
) -> DeviceReport:
    """Return a useful report when no registered manufacturer matched."""
    if discovery.mcu_rom.table is None:
        assert discovery.mcu_rom.unavailable_reason is not None
        reason = f"MCU-ROM identity unavailable: {discovery.mcu_rom.unavailable_reason}"
    else:
        peripheral_id = discovery.mcu_rom.table.identity.peripheral_id
        if peripheral_id.jep106 is None:
            reason = "MCU-ROM root does not advertise a JEDEC manufacturer identity"
        else:
            reason = (
                "no registered vendor profile matches MCU-ROM "
                f"{peripheral_id.jep106.display()}, part 0x{peripheral_id.part_number:03X}"
            )
    return DeviceReport(
        target=target,
        discovery=discovery,
        vendor="Generic Cortex-M",
        product_line=FieldValue.unavailable(reason),
        part_number=FieldValue.unavailable(reason),
        ram=FieldValue.unavailable(reason),
        flash=FieldValue.unavailable(reason),
        package=FieldValue.unavailable(reason),
        serial_number=FieldValue.unavailable(reason),
    )
