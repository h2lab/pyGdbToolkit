# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Portable architecture detection and system-register inspection APIs."""

from .arm.probe import DEFAULT_ARM_PROBES
from .base import (
    Architecture,
    ArchitectureProbe,
    ProbeResult,
    RegisterValue,
    SystemRegisterSet,
    TargetDescription,
)
from .registry import ArchitectureRegistry

DEFAULT_ARCHITECTURE_REGISTRY = ArchitectureRegistry(DEFAULT_ARM_PROBES)

__all__ = [
    "Architecture",
    "ArchitectureProbe",
    "ArchitectureRegistry",
    "DEFAULT_ARCHITECTURE_REGISTRY",
    "ProbeResult",
    "RegisterValue",
    "SystemRegisterSet",
    "TargetDescription",
]
