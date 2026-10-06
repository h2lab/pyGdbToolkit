# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Architecture identity supplied by GDB for an AArch64 target."""

from __future__ import annotations

from dataclasses import dataclass

from ..base import TargetDescription


@dataclass(frozen=True)
class AArch64TargetDescription(TargetDescription):
    """Minimal identity without inferring a CPU model or revision."""

    gdb_architecture: str
