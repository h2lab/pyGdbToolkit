# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Basic import tests for pyGdbToolkit."""

from importlib.util import find_spec


def test_import() -> None:
    """Test that package can be imported."""
    import pyGdbToolkit

    assert pyGdbToolkit is not None


def test_legacy_arm_modules_are_removed() -> None:
    """Arm modules are exposed only from the architecture namespace."""
    for module_name in ("coresight", "cpuid", "models", "providers"):
        assert find_spec(f"pyGdbToolkit.{module_name}") is None
