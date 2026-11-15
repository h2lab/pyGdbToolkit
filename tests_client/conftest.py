# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Reject GDB, server and toolkit target-side imports in client-level tests.

This suite must run in its own pytest process: the GDB-side suite in ``tests/``
installs a fake ``gdb`` module that would otherwise mask a forbidden dependency.
Only the toolkit's host-safe ``shared`` namespace is permitted.
"""

from client_isolation import install_import_guard

install_import_guard()
