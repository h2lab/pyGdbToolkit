# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Stable public debug-server detection API and backend namespace."""

from .detection import (
    OCD,
    OcdDetector,
    OcdIdentifier,
    OcdInfo,
    get_ocd,
    initialize_ocd,
    probe_ocd,
)

__all__ = [
    "OCD",
    "OcdDetector",
    "OcdIdentifier",
    "OcdInfo",
    "get_ocd",
    "initialize_ocd",
    "probe_ocd",
]
