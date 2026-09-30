# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""RTOS implementations available to GDB commands."""

from . import camelot

SUPPORTED_RTOS = {camelot.NAME: camelot}
