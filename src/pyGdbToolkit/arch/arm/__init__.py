# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Curated Arm target-description and probe integration surface.

Import Arm implementation APIs from their defining submodules, such as
``arch.arm.cortex_m`` or ``arch.arm.mpu``.
"""

from .target import ArmProfile, ArmTargetDescription
from .cortex_m import CortexMProbe

__all__ = [
    "ArmProfile",
    "ArmTargetDescription",
    "CortexMProbe",
]
