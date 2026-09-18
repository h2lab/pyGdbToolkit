# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Arm architecture probe entry points."""

from __future__ import annotations

from .cortex_m import CortexMProbe

DEFAULT_ARM_PROBES = (CortexMProbe(),)
