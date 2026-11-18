# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Typed GDB-side RTOS contracts, separate from host-safe project discovery."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rich.console import Console, Group

from ..shared.rtos import TaskListSnapshot


@dataclass(frozen=True)
class RtosCapabilities:
    """Operations supported by one loaded build, independently of live target state."""

    task_inspection: bool
    scheduler_trace: bool
    thread_enumeration: bool = False
    object_inspection: bool = False


class SchedulerTrace(Protocol):
    """An implementation-owned trace whose deletion releases all its breakpoints."""

    @property
    def limit(self) -> int:
        """Return the requested number of observations."""
        ...

    @property
    def elections(self) -> list[str]:
        """Return the observations collected so far."""
        ...

    def delete(self) -> None:
        """Cancel tracing, release breakpoints and notify the session."""
        ...


class RtosProject(Protocol):
    """Cached build context with explicit live inspection and tracing operations."""

    @property
    def capabilities(self) -> RtosCapabilities:
        """Return the operations supported by this build."""
        ...

    def task_list_snapshot(self) -> TaskListSnapshot | None:
        """Serialize cached static task slots, or None when not applicable.

        This must not access the target or filesystem. A loaded project need
        not have a static task table; live threads do not belong in this snapshot.

        Returns
        -------
        TaskListSnapshot | None
            Cached static slots, or None if the RTOS has no static task table.
        """
        ...

    def show_project(self) -> Group:
        """Render build-time information without reading the target."""
        ...

    def show_task(self, name: str) -> Group:
        """Inspect one task on the stopped target.

        Parameters
        ----------
        name : str
            Implementation-specific task identifier.

        Returns
        -------
        Group
            Live task information, with unavailable measurements labeled explicitly.

        Raises
        ------
        ValueError
            If the identifier is unknown or required metadata is invalid.
        """
        ...

    def show_threads(self) -> Group:
        """Enumerate live threads, explicitly labeling any incomplete coverage.

        Returns
        -------
        Group
            Thread identities and states sampled on the stopped target.

        Raises
        ------
        ValueError
            If enumeration is unsupported or required target data is inaccessible.
        """
        ...

    def show_object(self, kind: str, address: str) -> Group:
        """Inspect a typed synchronization object on the stopped target.

        Parameters
        ----------
        kind : str
            Implementation-specific object type, not a GDB expression.
        address : str
            Hexadecimal object address.

        Returns
        -------
        Group
            Sampled object state and explicit waiter coverage.

        Raises
        ------
        ValueError
            If inspection is unsupported, input is invalid or data is unreadable.
        """
        ...

    def trace_scheduler(
        self, limit: int, console: Console, on_delete: Callable[[SchedulerTrace], None]
    ) -> SchedulerTrace:
        """Start implementation-specific observation of the scheduler.

        Parameters
        ----------
        limit : int
            Positive number of observations to collect.
        console : Console
            Destination for trace results and explicit asynchronous failures.
        on_delete : Callable[[SchedulerTrace], None]
            Called when the trace releases its breakpoints, including on failure.

        Returns
        -------
        SchedulerTrace
            Trace owned by the session until it finishes or is cancelled.

        Raises
        ------
        ValueError
            If the limit is invalid or the build lacks required scheduler metadata.
        """
        ...


class RtosImplementation(Protocol):
    """Discover symbols and build a typed context for one RTOS."""

    @property
    def name(self) -> str:
        """Return the identifier shared with the host-safe project provider."""
        ...

    def project_elfs(self, project_path: Path) -> list[Path]:
        """Validate the project and return its ELF files, primary image first.

        RTOS-specific image requirements must be checked before GDB loads symbols.

        Parameters
        ----------
        project_path : Path
            Resolved build or project root on the GDB host.

        Returns
        -------
        list[Path]
            Nonempty, ordered symbol files within the project root.

        Raises
        ------
        OSError
            If required project files cannot be read.
        ValueError
            If the project does not satisfy the implementation's image requirements.
        """
        ...

    def load_project(self, project_path: Path, primary_elf: Path) -> RtosProject:
        """Decode build metadata after symbols have been loaded, without live reads.

        Parameters
        ----------
        project_path : Path
            Resolved build or project root on the GDB host.
        primary_elf : Path
            First image returned by project_elfs, with symbols already loaded.

        Returns
        -------
        RtosProject
            Cached build context, independent of static task-table availability.

        Raises
        ------
        OSError
            If required build metadata cannot be read.
        ValueError
            If the build metadata is invalid.
        """
        ...
