# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Select host-side OCD adapters without coupling the MI transport to a backend."""

from ..config import ServerConfig
from ..logs import LogStore
from ..mi import MiSession
from .qemu import QemuMiSession


def create_mi_session(config: ServerConfig, logs: LogStore) -> MiSession:
    """Create the MI adapter for the configured OCD backend.

    The QEMU backend uses ``QemuMiSession`` to handle its monitor commands.
    All other backends default to ``MiSession``, the generic OCD MI adapter.

    Parameters
    ----------
    config : ServerConfig
        Configuration identifying the OCD backend and GDB executable arguments.
    logs : LogStore
        Shared store for the adapter's GDB logs.

    Returns
    -------
    MiSession
        ``QemuMiSession`` for QEMU, otherwise the generic ``MiSession`` adapter.
    """
    adapters: dict[str, type[MiSession]] = {"qemu": QemuMiSession}
    session_type = adapters.get(config.backend_identifier, MiSession)
    return session_type(config.gdb_path, config.gdb_args, logs)
