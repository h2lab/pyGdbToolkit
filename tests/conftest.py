"""Install a deterministic GDB module before package commands are imported."""

from __future__ import annotations

import sys
import types
from shlex import split
from collections.abc import Iterator
from typing import Any

import pytest


class FakeGdbError(Exception):
    """Exception exposed by the mock GDB module."""


class FakeCommand:
    """Minimal GDB command base class for command-registration tests."""

    registrations: list[tuple[str, int]] = []

    def __init__(self, name: str, command_class: int, *args: object) -> None:
        """Record a command registration."""
        del args
        self.registrations.append((name, command_class))


class FakeBreakpoint:
    """Minimal GDB breakpoint base class used by SVD watchpoint registration."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Accept the breakpoint arguments used by package command classes."""
        del args
        del kwargs


_gdb = types.ModuleType("gdb")
_gdb.COMMAND_USER = 0
_gdb.COMPLETE_NONE = 0
_gdb.BP_WATCHPOINT = 0
_gdb.WP_WRITE = 0
_gdb.Command = FakeCommand
_gdb.Breakpoint = FakeBreakpoint
_gdb.GdbError = FakeGdbError
_gdb.error = FakeGdbError
_gdb.MemoryError = FakeGdbError
_gdb.Inferior = object
_gdb._inferior = None


def _selected_inferior() -> Any:
    """Return the selected fake inferior."""
    if _gdb._inferior is None:
        raise FakeGdbError("no inferior selected")
    return _gdb._inferior


_gdb.selected_inferior = _selected_inferior
_gdb.string_to_argv = split
sys.modules["gdb"] = _gdb


@pytest.fixture(autouse=True)
def reset_fake_gdb() -> Iterator[None]:
    """Reset mock GDB state between tests."""
    _gdb._inferior = None
    FakeCommand.registrations.clear()
    yield
    _gdb._inferior = None


@pytest.fixture
def fake_gdb() -> types.ModuleType:
    """Provide the GDB module installed before package import."""
    return _gdb
