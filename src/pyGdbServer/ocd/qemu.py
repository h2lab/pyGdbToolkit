# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Host-side QEMU OCD adapter for checked HMP console requests."""

import json
import re

from ..mi import MiResult, MiSession, _decode_mi_string


class QemuMiSession(MiSession):
    """Route QEMU monitor requests through its GDB-side OCD error handling."""

    async def execute(self, command: str, timeout: float = 30.0) -> MiResult:
        """Adapt console monitor requests before delegating to the neutral MI transport."""
        match = re.fullmatch(r'-interpreter-exec\s+(?:"console"|console)\s+(.+)', command)
        if match is not None:
            console = _decode_mi_string(match[1].strip())
            parts = console.split(maxsplit=1)
            if parts and parts[0] in ("mon", "moni", "monit", "monito", "monitor"):
                arguments = parts[1] if len(parts) == 2 else ""
                console = f"pygdb-qemu-monitor {arguments}"
                command = f"-interpreter-exec console {json.dumps(console)}"
        return await super().execute(command, timeout)
