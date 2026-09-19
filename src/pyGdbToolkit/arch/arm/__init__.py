# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Arm target-description support."""

from .cortex_m import CortexMProbe, CortexMTargetDescription, inspect_cortex_m
from .mpu import (
    MpuArchitecture,
    MpuDescription,
    MpuDump,
    MpuRegion,
    MpuRegionResult,
    dump_mpu_regions,
)
from .target import ArmProfile, ArmTargetDescription

__all__ = [
    "ArmProfile",
    "ArmTargetDescription",
    "CortexMProbe",
    "CortexMTargetDescription",
    "MpuArchitecture",
    "MpuDescription",
    "MpuDump",
    "MpuRegion",
    "MpuRegionResult",
    "dump_mpu_regions",
    "inspect_cortex_m",
]
