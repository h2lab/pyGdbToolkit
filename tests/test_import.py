# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Basic import tests for pyGdbToolkit."""

from importlib.util import find_spec


def test_import() -> None:
    """Test that package can be imported."""
    import pyGdbToolkit

    assert pyGdbToolkit is not None


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
