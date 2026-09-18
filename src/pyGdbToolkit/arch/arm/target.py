# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Shared Arm target-description types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..base import Architecture, TargetDescription


class ArmProfile(StrEnum):
    """Architectural execution profiles implemented by Arm cores."""

    CORTEX_M = "cortex-m"
    CORTEX_A = "cortex-a"
    CORTEX_R = "cortex-r"


@dataclass(frozen=True)
class ArmTargetDescription(TargetDescription):
    """Target identity common to all Arm architecture profiles."""

    profile: ArmProfile

    def __post_init__(self) -> None:
        """Require Arm target descriptions to retain the Arm architecture tag."""
        if self.architecture is not Architecture.ARM:
            raise ValueError("an Arm target description must use the Arm architecture")
