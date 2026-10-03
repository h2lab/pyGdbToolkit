# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Read-only ARM ADIv5/APv1 and ADIv6/APv2 capabilities."""

from __future__ import annotations

from ...debug_port import AccessPort, DebugPortError, DebugPortTransport
from ..dap import AccessPortProfile


class ArmAccessPortProvider:
    """Decode ARM AP identity without writing CSW, TAR or target memory."""

    def supports(self, architecture: str) -> bool:
        """Accept ARM and AArch64 GDB architecture names."""
        return architecture.lower().startswith(("arm", "aarch64"))

    def profile(self, transport: DebugPortTransport, port: AccessPort) -> AccessPortProfile:
        """Read IDR and applicable MEM-AP registers, retaining individual failures."""
        registers: dict[str, int] = {}
        errors: dict[str, str] = {}
        identity: dict[str, int | str] = {}
        capabilities: dict[str, bool | int | str | None] = {}

        def read(name: str, address: int) -> int | None:
            try:
                value = transport.read_ap(port.index, address)
            except DebugPortError as error:
                errors[name] = str(error)
                return None
            registers[name] = value
            return value

        offset = 0xD00 if port.ap_version == 2 else 0
        idr = read("IDR", offset + 0xFC)
        type_name = "unknown"
        if idr in (None, 0, 0xFFFFFFFF):
            if idr is not None:
                errors["IDR"] = "No valid AP identity returned"
        else:
            designer = (idr >> 17) & 0x7FF
            ap_class = (idr >> 13) & 0xF
            ap_type = idr & 0xF
            identity.update(
                ap_version=port.ap_version,
                designer_jep106=designer,
                designer="ARM" if designer == 0x23B else "unknown",
                ap_class=ap_class,
                ap_type=ap_type,
                variant=(idr >> 4) & 0xF,
                revision=(idr >> 28) & 0xF,
            )
            memory_access = ap_class == 8
            capabilities["memory_access"] = memory_access
            if memory_access:
                type_name = "MEM-AP"
                if designer == 0x23B:
                    type_name = {
                        1: "AHB-AP",
                        2: "APB-AP",
                        4: "AXI-AP",
                        5: "AHB5-AP",
                        6: "APB5-AP",
                        7: "AXI5-AP",
                        8: "AHB5-AP (extended HPROT)",
                    }.get(ap_type, "MEM-AP (unknown bus)")
                cfg = read("CFG", offset + 0xF4)
                csw = read("CSW", offset)
                base = read("BASE", offset + 0xF8)
                capabilities.update(
                    large_address=None if cfg is None else bool(cfg & 2),
                    large_data=None if cfg is None else bool(cfg & 4),
                    device_enabled=None if csw is None else bool(csw & 0x40),
                    rom_table_present=None,
                    rom_table_address=None,
                )
                if port.ap_version == 2:
                    capabilities.update(
                        error_mode=None if cfg is None else (cfg >> 8) & 0xF,
                        dar_window_bytes=None if cfg is None else 1 << ((cfg >> 4) & 0xF),
                        auto_increment_page_bytes=(
                            None
                            if cfg is None
                            else 1 << (9 + ((cfg >> 16) & 0xF)) if (cfg >> 16) & 0xF else 1024
                        ),
                    )
                if csw is not None:
                    capabilities["current_transfer_size_bits"] = 8 << (csw & 7)
                    capabilities["current_address_increment"] = (csw >> 4) & 3
                if base is not None:
                    present = base != 0xFFFFFFFF and (not (base & 2) or bool(base & 1))
                    capabilities["rom_table_present"] = present
                    if present:
                        address: int | None = base & (0xFFFFFFFC if base & 2 else 0xFFFFF000)
                        if cfg is None:
                            address = None
                        elif cfg & 2:
                            high = read("BASE2", offset + 0xF0)
                            address = None if high is None else (base & 0xFFFFFFFC) | (high << 32)
                        capabilities["rom_table_address"] = address
                capabilities["operations"] = (
                    "Memory access via MEM-AP; access depends on security and bus permissions"
                )
            elif designer == 0x23B and ap_class == 0 and ap_type == 0:
                type_name = "JTAG-AP"
                capabilities["operations"] = (
                    "Downstream JTAG scan-chain access (not exposed by this backend)"
                )
            elif designer == 0x23B and ap_class == 1:
                type_name = "COM-AP"
                capabilities["operations"] = "Debug communication (not exposed by this backend)"
        return AccessPortProfile(
            port.index, "arm", type_name, identity, capabilities, registers, errors
        )
