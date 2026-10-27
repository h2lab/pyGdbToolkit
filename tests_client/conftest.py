# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Make GDB, toolkit, and server modules unavailable to client-level tests.

This suite must run in its own pytest process: the GDB-side suite in ``tests/``
installs a fake ``gdb`` module that would otherwise mask a forbidden dependency.
"""

from client_isolation import install_import_guard

install_import_guard()
