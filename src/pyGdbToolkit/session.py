# SPDX-FileCopyrightText: 2026 H2Lab Development Team
#
# SPDX-License-Identifier: Apache-2.0

"""Unified, architecture-neutral session context shared by every GDB command.

Commands do not own their state anymore: they request it from the process-wide
:data:`SESSION` object, which owns target access, architecture identification,
the per-command state slices registered through :meth:`ToolkitSession.state`,
and the command help registered through :meth:`ToolkitSession.register_command`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

import gdb

from .arch import (
    DEFAULT_ARCHITECTURE_REGISTRY,
    DEFAULT_DIAGNOSTIC_RUNTIME,
    Architecture,
    ArchitectureRegistry,
    DiagnosticResult,
    DiagnosticRuntime,
    DiagnosticRuntimeAccess,
    DiagnosticServiceName,
    ProbeResult,
    TargetDescription,
)
from .target_memory import TargetMemoryReader, WritableTargetMemory
from .arch.memmap import MemoryRegion, TargetFingerprint
from .memmap import MemoryMapReport


class SessionSlice(ABC):
    """One command-owned state fragment stored and maintained by the session.

    A slice is created once per GDB session and is never replaced, so command
    modules may hold a direct reference to it.  Clearing a slice therefore has
    to happen in place, through :meth:`reset`.
    """

    @abstractmethod
    def reset(self) -> None:
        """Clear the slice in place and release its target-dependent resources."""


@dataclass(frozen=True)
class CommandUsage:
    """One syntax line of a command reference and its description."""

    syntax: str
    description: str


@dataclass(frozen=True)
class CommandHelp:
    """Self-describing help of one top-level pyGdbToolkit command."""

    name: str
    summary: str
    usage: tuple[CommandUsage, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable view of the command help."""
        return {
            "name": self.name,
            "summary": self.summary,
            "usage": [
                {"syntax": entry.syntax, "description": entry.description} for entry in self.usage
            ],
            "notes": list(self.notes),
        }


SliceT = TypeVar("SliceT", bound=SessionSlice)
TargetT = TypeVar("TargetT", bound=TargetDescription)


class ToolkitSession:
    """Own the target access, the architecture identity, and the command state slices."""

    def __init__(
        self,
        architecture_registry: ArchitectureRegistry = DEFAULT_ARCHITECTURE_REGISTRY,
        diagnostic_runtime: DiagnosticRuntime = DEFAULT_DIAGNOSTIC_RUNTIME,
    ) -> None:
        """Create a session bound to an architecture registry and a diagnostic runtime.

        Parameters
        ----------
        architecture_registry : ArchitectureRegistry
            Registry probing the supported architectures, in priority order.
        diagnostic_runtime : DiagnosticRuntime
            Runtime dispatching diagnostic services to the detected architecture.
        """
        self._architecture_registry = architecture_registry
        self._diagnostic_runtime = diagnostic_runtime
        self._memory: WritableTargetMemory | None = None
        self._probe: ProbeResult | None = None
        self._slices: dict[type[SessionSlice], SessionSlice] = {}
        self._commands: dict[str, CommandHelp] = {}
        self._discovery: MemoryMapReport | None = None
        self._discovery_context: Callable[[], dict[str, Any]] | None = None

    def publish_discovery(
        self, report: MemoryMapReport, context: Callable[[], dict[str, Any]]
    ) -> bool:
        """Share confirmed identity and memory evidence for the current target only."""
        self._discovery = None
        self._discovery_context = None
        if report.fingerprint is None or not report.fingerprint.confirmed:
            return False
        if context() != report.context:
            return False
        self._discovery = report
        self._discovery_context = context
        return True

    @property
    def discovery(self) -> MemoryMapReport | None:
        """The confirmed discovery, discarded when its access context changes."""
        if self._discovery is not None and self._discovery_context is not None:
            try:
                current = self._discovery_context()
            except (gdb.error, RuntimeError):
                current = None
            if current != self._discovery.context:
                self._discovery = None
                self._discovery_context = None
        return self._discovery

    @property
    def target_info(self) -> TargetFingerprint | None:
        """Supplement architectural identity with a confirmed vendor/SoC fingerprint."""
        discovery = self.discovery
        return None if discovery is None else discovery.fingerprint

    @property
    def memory_regions(self) -> tuple[MemoryRegion, ...]:
        """Declared regions of the confirmed target; candidates are kept separate."""
        discovery = self.discovery
        return () if discovery is None else tuple(discovery.regions)

    def register_command(self, command_help: CommandHelp) -> None:
        """Record the help of a toolkit command registered in GDB.

        Parameters
        ----------
        command_help : CommandHelp
            Help describing the command; replaces any previous entry of the same name.
        """
        self._commands[command_help.name] = command_help

    @property
    def commands(self) -> tuple[CommandHelp, ...]:
        """The help of every registered toolkit command, in registration order."""
        return tuple(self._commands.values())

    @property
    def memory(self) -> WritableTargetMemory:
        """The target-memory accessor shared by every command.

        Returns
        -------
        WritableTargetMemory
            Accessor bound to the inferior selected when the session was populated.
        """
        if self._memory is None:
            self._memory = TargetMemoryReader()
        return self._memory

    def probe(self) -> ProbeResult:
        """Identify the connected target once per session.

        Returns
        -------
        ProbeResult
            The detected target description or an explicit unavailable result.
        """
        if self._probe is None:
            self._probe = self._architecture_registry.probe(self.memory)
        return self._probe

    @property
    def architecture(self) -> Architecture | None:
        """The architecture of the connected target, or ``None`` when unidentified."""
        target = self.probe().target
        return None if target is None else target.architecture

    def require_target(self) -> TargetDescription:
        """Return the detected target description.

        Returns
        -------
        TargetDescription
            The architecture-neutral identity of the connected target.

        Raises
        ------
        gdb.GdbError
            If no registered architecture probe recognized the target.
        """
        result = self.probe()
        if result.target is None:
            raise gdb.GdbError(result.unavailable_reason or "target unavailable")
        return result.target

    def require_target_of(self, description_type: type[TargetT]) -> TargetT:
        """Return the detected target description narrowed to an architecture model.

        Parameters
        ----------
        description_type : type[TargetT]
            Architecture-specific description class expected by the caller.

        Returns
        -------
        TargetT
            The detected target description.

        Raises
        ------
        gdb.GdbError
            If the target is unknown or is not described by ``description_type``.
        """
        target = self.require_target()
        if not isinstance(target, description_type):
            raise gdb.GdbError(
                f"target '{target.core_name}' is not supported by this command "
                f"(expected a {description_type.__name__} target)"
            )
        return target

    def diagnose(
        self,
        service: DiagnosticServiceName,
        access: DiagnosticRuntimeAccess | None = None,
    ) -> DiagnosticResult:
        """Run one diagnostic service against the session target.

        Parameters
        ----------
        service : DiagnosticServiceName
            Diagnostic service requested by the calling command.
        access : DiagnosticRuntimeAccess | None
            Optional register and symbol access handed to the service.

        Returns
        -------
        DiagnosticResult
            The collected report or an explicit unavailable result.
        """
        return self._diagnostic_runtime.diagnose(self.memory, service, access)

    def state(self, slice_type: type[SliceT]) -> SliceT:
        """Return the session-owned instance of a command state slice.

        The slice is created on first request and kept for the whole session, so
        a command module can bind it once at import time.

        Parameters
        ----------
        slice_type : type[SliceT]
            Slice class registered by the calling command.

        Returns
        -------
        SliceT
            The unique instance of ``slice_type`` for this session.

        Raises
        ------
        TypeError
            If ``slice_type`` does not build an instance of itself.
        """
        instance = self._slices.get(slice_type)
        if instance is None:
            instance = slice_type()
            self._slices[slice_type] = instance
        if not isinstance(instance, slice_type):
            raise TypeError(f"session slice '{slice_type.__name__}' has an inconsistent type")
        return instance

    def invalidate(self) -> None:
        """Drop the cached target access and identity without clearing command state."""
        self._memory = None
        self._probe = None
        self._discovery = None
        self._discovery_context = None

    def reset(self) -> None:
        """Drop the cached target data and clear every registered state slice."""
        self.invalidate()
        for state_slice in self._slices.values():
            state_slice.reset()


SESSION = ToolkitSession()


def install_event_hooks(session: ToolkitSession = SESSION) -> None:
    """Keep a session coherent with the GDB events that change the target.

    Parameters
    ----------
    session : ToolkitSession
        Session whose cached target access and identity must be invalidated.
    """
    events = getattr(gdb, "events", None)
    if events is None:
        return

    def _invalidate(event: object) -> None:
        del event
        session.invalidate()

    for event_name in ("exited", "new_objfile", "clear_objfiles"):
        event_registry = getattr(events, event_name, None)
        if event_registry is not None:
            event_registry.connect(_invalidate)
