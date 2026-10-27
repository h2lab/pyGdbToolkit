# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Import guard keeping client tests independent of target-side packages."""

from __future__ import annotations

from collections.abc import Sequence
import importlib.abc
import importlib.machinery
import sys
from types import ModuleType

FORBIDDEN_PACKAGES = frozenset({"gdb", "pyGdbToolkit", "pyGdbServer"})


class RejectTargetDependencies(importlib.abc.MetaPathFinder):
    """Fail imports of target-side packages as if they were not installed."""

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        """Reject forbidden packages and defer every other import."""
        del path, target
        if fullname.partition(".")[0] in FORBIDDEN_PACKAGES:
            raise ModuleNotFoundError(f"{fullname} is unavailable to client tests", name=fullname)
        return None


def loaded_forbidden_modules() -> list[str]:
    """Return already-imported target-side modules."""
    return sorted(name for name in sys.modules if name.partition(".")[0] in FORBIDDEN_PACKAGES)


def install_import_guard() -> None:
    """Fail if target-side modules are loaded, then reject later imports of them."""
    loaded = loaded_forbidden_modules()
    if loaded:
        raise RuntimeError(
            "client tests must run in a separate pytest process (for example "
            f"'tox -e client'); already imported: {', '.join(loaded)}"
        )
    if not any(isinstance(finder, RejectTargetDependencies) for finder in sys.meta_path):
        sys.meta_path.insert(0, RejectTargetDependencies())
