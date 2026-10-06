# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""OpenOCD named hardware-thread CPU selection strategy."""

from __future__ import annotations

import re

import gdb

from .base import CoreBackend, CoreInfo
from .detection import OcdIdentifier


def read_external_debug_word(offset: int) -> int | None:
    """Read a CPU debug word through an explicitly configured matching MEM-AP view."""
    selected_thread = getattr(gdb, "selected_thread", None)
    if selected_thread is None:
        return None
    try:
        thread = selected_thread()
    except gdb.error:
        return None
    name = "" if thread is None else thread.name or ""
    if re.fullmatch(r"[A-Za-z0-9_.-]+", name) is None:
        return None
    try:
        current = str(gdb.execute("monitor echo [target current]", to_string=True)).strip()
        if current != name:
            return None
        metadata = (
            str(
                gdb.execute(
                    f"monitor echo [list [{name} cget -type] [{name} cget -dbgbase] "
                    f"[{name} cget -dap] [{name} cget -ap-num]]",
                    to_string=True,
                )
            )
            .strip()
            .split()
        )
        if len(metadata) != 4 or metadata[0] != "aarch64":
            return None
        base = int(metadata[1], 0)
        if base <= 0 or base & 0xFFF:
            return None
        targets = str(gdb.execute("monitor echo [target names]", to_string=True)).strip().split()
        matches = []
        for target in targets:
            if re.fullmatch(r"[A-Za-z0-9_.-]+", target) is None:
                return None
            kind = str(gdb.execute(f"monitor echo [{target} cget -type]", to_string=True)).strip()
            if kind != "mem_ap":
                continue
            view = (
                str(
                    gdb.execute(
                        f"monitor echo [list [{target} cget -dap] [{target} cget -ap-num]]",
                        to_string=True,
                    )
                )
                .strip()
                .split()
            )
            if view == metadata[2:]:
                matches.append(target)
        if len(matches) != 1:
            return None
        output = str(
            gdb.execute(
                f"monitor echo [{matches[0]} read_memory 0x{base + offset:X} 32 1]",
                to_string=True,
            )
        ).strip()
        if re.fullmatch(r"0x[0-9a-fA-F]{1,8}", output) is None:
            return None
        return int(output, 16)
    except (gdb.error, ValueError):
        return None


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
        """Synchronize the GDB hardware thread and OpenOCD's monitor target."""
        if re.fullmatch(r"[A-Za-z0-9_.-]+", core.name) is None:
            raise gdb.GdbError("OpenOCD hardware target name is unsafe for monitor selection")
        previous = gdb.selected_thread()
        thread = next(
            thread
            for thread in gdb.selected_inferior().threads()
            if thread.global_num == core.thread
        )
        try:
            thread.switch()
            gdb.execute(f"monitor targets {core.name}", to_string=True)
            output = str(gdb.execute("monitor echo [target current]", to_string=True)).strip()
            if output != core.name:
                raise gdb.GdbError(f"OpenOCD did not select monitor target {core.name}")
        except Exception:
            if previous is not None:
                previous.switch()
                name = previous.name or ""
                if re.fullmatch(r"[A-Za-z0-9_.-]+", name) is not None:
                    gdb.execute(f"monitor targets {name}", to_string=True)
            raise
