# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""RTOS contracts usable by GDB, pyGdbServer and pyGdbClient."""

from .models import (
    ProjectManifest,
    RtosSnapshot,
    SchedulerSnapshot,
    TaskListSnapshot,
    TaskSlotSnapshot,
    matches_project_pattern,
    project_relative_path,
)
from .registry import (
    DEFAULT_PROJECT_REGISTRY,
    ProjectProvider,
    ProjectRegistry,
    project_manifest,
)

__all__ = [
    "DEFAULT_PROJECT_REGISTRY",
    "ProjectManifest",
    "ProjectProvider",
    "ProjectRegistry",
    "RtosSnapshot",
    "SchedulerSnapshot",
    "TaskListSnapshot",
    "TaskSlotSnapshot",
    "matches_project_pattern",
    "project_manifest",
    "project_relative_path",
]
