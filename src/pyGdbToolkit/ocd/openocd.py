# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""OpenOCD named hardware-thread CPU selection strategy."""

from __future__ import annotations

import re

import gdb

from .base import CoreBackend, CoreInfo
from .detection import OcdIdentifier


class OpenOcdCoreBackend(CoreBackend):
    """Select physical CPUs exposed as hardware threads on a shared connection."""

    identifier = OcdIdentifier.OPENOCD

    def list_cores(self) -> tuple[CoreInfo, ...]:
        """Map hardware-thread names to physical IDs without relying on thread order."""
        inferior = gdb.selected_inferior()
        selected = gdb.selected_thread()
        cores = []
        for thread in inferior.threads():
            name = thread.name or ""
            match = re.search(r"(?:^|[.])(?:cm|cpu|core|rv)(\d+)$", name)
            if match is None:
                raise gdb.GdbError(
                    "OpenOCD must expose named hardware-core threads in SMP mode; "
                    f"cannot identify core for thread {thread.global_num} ({name!r})"
                )
            cores.append(
                CoreInfo(
                    int(match[1]),
                    name,
                    thread == selected,
                    str(self.context.connection().details),
                    inferior.num,
                    thread.global_num,
                )
            )
        if not cores or len({core.id for core in cores}) != len(cores):
            raise gdb.GdbError("OpenOCD did not expose unique hardware-core threads")
        return tuple(sorted(cores, key=lambda core: core.id))

    def select_core(self, core: CoreInfo) -> None:
        """Switch the hardware thread corresponding to the requested core."""
        thread = next(
            thread
            for thread in gdb.selected_inferior().threads()
            if thread.global_num == core.thread
        )
        thread.switch()
