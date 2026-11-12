# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Allow shared contracts but reject GDB, server and target-side toolkit services."""

import importlib
import sys

import pytest

from client_isolation import FORBIDDEN_PACKAGES, loaded_forbidden_modules


@pytest.mark.parametrize(
    "module",
    ["gdb", "pyGdbToolkit.session", "pyGdbToolkit.cmd_rtos", "pyGdbToolkit.rtos", "pyGdbServer"],
)
def test_target_side_packages_are_unavailable(module: str) -> None:
    """Target-side imports fail instead of resolving to a fake or installed module."""
    with pytest.raises(ModuleNotFoundError, match="unavailable to client tests"):
        importlib.import_module(module)


@pytest.mark.parametrize(
    "module",
    [
        "pyGdbClient",
        "pyGdbClient.app",
        "pyGdbClient.cli",
        "pyGdbClient.rpc",
        "pyGdbClient.llm.contracts",
        "pyGdbClient.llm.provider",
        "pyGdbClient.llm.scripted",
    ],
)
def test_client_modules_import_without_target_side_packages(module: str) -> None:
    """Every client module imports while target-side packages are blocked."""
    importlib.import_module(module)
    assert loaded_forbidden_modules() == []


@pytest.mark.parametrize(
    "module",
    [
        "pyGdbToolkit.shared",
        "pyGdbToolkit.shared.rtos",
        "pyGdbToolkit.shared.rtos.models",
        "pyGdbToolkit.shared.rtos.registry",
        "pyGdbToolkit.shared.rtos.camelot",
    ],
)
def test_shared_modules_import_without_target_side_dependencies(module: str) -> None:
    """Only the shared toolkit namespace is available to client code."""
    importlib.import_module(module)
    assert loaded_forbidden_modules() == []
    assert "gdb" not in sys.modules
    assert not hasattr(sys.modules["pyGdbToolkit"], "SESSION")


def test_no_fake_gdb_module_is_installed() -> None:
    """The GDB-side test shim is absent from this process."""
    assert "gdb" not in sys.modules
    assert FORBIDDEN_PACKAGES == {"gdb", "pyGdbToolkit", "pyGdbServer"}
