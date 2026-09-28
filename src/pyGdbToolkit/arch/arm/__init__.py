# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Arm target-description, CoreSight, and device-provider support."""

from .coresight import CoreSightDiscovery, RomTableDiscovery, discover_rom_tables
from .cortex_m import (
    CPUID_ADDRESS,
    CortexMProbe,
    CortexMTargetDescription,
    decode_cpuid,
    inspect_cortex_m,
)
from .models import DeviceReport, FieldValue
from .mpu import (
    MpuArchitecture,
    MpuDescription,
    MpuDump,
    MpuRegion,
    MpuRegionResult,
    dump_mpu_regions,
)
from .sau import SauAttribution, SauDescription, SauDump, SauRegion, SauStatus, dump_sau_regions
from .target import ArmProfile, ArmTargetDescription
from .providers import DEFAULT_PROVIDER_REGISTRY, DeviceProvider, ProviderRegistry

__all__ = [
    "ArmProfile",
    "ArmTargetDescription",
    "CPUID_ADDRESS",
    "CoreSightDiscovery",
    "CortexMProbe",
    "CortexMTargetDescription",
    "DEFAULT_PROVIDER_REGISTRY",
    "DeviceProvider",
    "DeviceReport",
    "FieldValue",
    "MpuArchitecture",
    "MpuDescription",
    "MpuDump",
    "MpuRegion",
    "MpuRegionResult",
    "ProviderRegistry",
    "RomTableDiscovery",
    "SauAttribution",
    "SauDescription",
    "SauDump",
    "SauRegion",
    "SauStatus",
    "decode_cpuid",
    "discover_rom_tables",
    "dump_mpu_regions",
    "dump_sau_regions",
    "inspect_cortex_m",
]
