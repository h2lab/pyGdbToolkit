# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Recognize NXP manufacturer identity without probing undocumented device registers."""

from ...coresight import MCU_ROM_TABLE_ADDRESS, CoreSightDiscovery, Jep106Identity
from ...cortex_m import CortexMTargetDescription
from ...models import DeviceReport, FieldValue
from .....target_memory import TargetMemory

NXP_JEP106_IDENTITY = Jep106Identity(bank=0, code=0x15)
FREESCALE_JEP106_IDENTITY = Jep106Identity(bank=0, code=0x0E)
NXP_JEP106_IDENTITIES = (NXP_JEP106_IDENTITY, FREESCALE_JEP106_IDENTITY)


class NxpProvider:
    """Identify NXP/legacy Freescale from the validated MCU-ROM root only."""

    def inspect(
        self,
        reader: TargetMemory,
        target: CortexMTargetDescription,
        discovery: CoreSightDiscovery,
    ) -> DeviceReport | None:
        """Reuse ROM evidence without reading signatures, OTP or peripheral registers."""
        del reader
        table = discovery.mcu_rom.table
        if table is None or table.base != MCU_ROM_TABLE_ADDRESS:
            return None
        peripheral = table.identity.peripheral_id
        if peripheral.jep106 not in NXP_JEP106_IDENTITIES:
            return None
        reason = (
            "NXP manufacturer identified from MCU-ROM; no documented device profile matches "
            f"{peripheral.jep106.display()}, part 0x{peripheral.part_number:03X}"
        )
        return DeviceReport(
            target=target,
            discovery=discovery,
            vendor="NXP Semiconductors",
            product_line=FieldValue.unavailable(reason),
            part_number=FieldValue.unavailable(reason),
            ram=FieldValue.unavailable(reason),
            flash=FieldValue.unavailable(reason),
            package=FieldValue.unavailable(reason),
            serial_number=FieldValue.unavailable(reason),
        )
