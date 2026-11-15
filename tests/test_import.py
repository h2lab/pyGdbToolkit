# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Basic import tests for pyGdbToolkit."""

from importlib.util import find_spec
from pathlib import Path
import subprocess
import sys

import pytest


def test_import() -> None:
    """Test that package can be imported."""
    import pyGdbToolkit

    assert pyGdbToolkit is not None


def test_toolkit_commands_register_their_help_in_the_session() -> None:
    """Every top-level command publishes its own help through the shared session."""
    from pyGdbToolkit import SESSION

    helps = {command.name: command for command in SESSION.commands}

    assert set(helps) >= {"lscpu", "fault_info", "rtos", "svd", "secscan", "memmap"}
    assert any(entry.syntax == "svd load" for entry in helps["svd"].usage)
    assert helps["rtos"].to_dict()["notes"]
    assert any(entry.syntax.startswith("memmap discover") for entry in helps["memmap"].usage)


@pytest.mark.parametrize("failure", ["gdb_dependency", "command_dependency"])
def test_optional_gdb_initialization_does_not_hide_import_failures(failure: str) -> None:
    """Only absence of gdb itself permits host-only imports; initialization errors propagate."""
    script = """
import importlib.abc
import sys
import types

failure = sys.argv[2]
if failure == "command_dependency":
    sys.modules["gdb"] = types.ModuleType("gdb")

class FailImport(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "gdb" or fullname == "pyGdbToolkit.cmd_dap":
            raise ModuleNotFoundError("missing initialization dependency", name="broken_dependency")
        return None

sys.path.insert(0, sys.argv[1])
sys.meta_path.insert(0, FailImport())
try:
    import pyGdbToolkit
except ModuleNotFoundError as error:
    assert error.name == "broken_dependency"
else:
    raise AssertionError("GDB initialization error was swallowed")
"""
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            script,
            str(Path(__file__).resolve().parents[1] / "src"),
            failure,
        ],
        check=True,
    )


def test_arm_package_exports_only_target_probe_integration() -> None:
    """The Arm package does not become a convenience barrel for implementation APIs."""
    import pyGdbToolkit.arch.arm as arm
    from pyGdbToolkit.arch.arm.cortex_m import CortexMProbe
    from pyGdbToolkit.arch.arm.target import ArmProfile, ArmTargetDescription

    assert arm.__all__ == ["ArmProfile", "ArmTargetDescription", "CortexMProbe"]
    assert arm.ArmProfile is ArmProfile
    assert arm.ArmTargetDescription is ArmTargetDescription
    assert arm.CortexMProbe is CortexMProbe
    assert not hasattr(arm, "CortexMSecurityAuditor")
    assert not hasattr(arm, "dump_mpu_regions")


def test_legacy_arm_modules_are_removed() -> None:
    """Arm modules are exposed only from the architecture namespace."""
    for module_name in ("coresight", "cpuid", "models", "providers"):
        assert find_spec(f"pyGdbToolkit.{module_name}") is None


def test_dap_module_paths_replace_ap_modules() -> None:
    """Portable and ARM DAP modules are available only under their new paths."""
    for package in ("pyGdbToolkit.arch", "pyGdbToolkit.arch.arm"):
        assert find_spec(f"{package}.dap") is not None
        assert find_spec(f"{package}.ap") is None


def test_dap_command_module_and_export_replace_ap_names() -> None:
    """The command uses DAP naming in its module and package-level API."""
    import pyGdbToolkit
    from pyGdbToolkit.cmd_dap import DapCmd

    assert pyGdbToolkit.DapCmd is DapCmd
    assert "DapCmd" in pyGdbToolkit.__all__
    assert not hasattr(pyGdbToolkit, "ApCmd")
    assert find_spec("pyGdbToolkit.cmd_ap") is None
