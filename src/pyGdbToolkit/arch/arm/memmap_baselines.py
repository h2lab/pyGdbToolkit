# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Broad ARM manufacturer candidate windows, not physical memory capacities."""

from __future__ import annotations

from ..memmap import MemoryBaseline


def _family(
    vendor: str,
    family: str,
    architecture: str,
    reference: str,
    windows: tuple[tuple[int, int, str, str], ...],
    aliases: tuple[str, ...] = (),
) -> tuple[MemoryBaseline, ...]:
    """Attach family and reference metadata to every representative window."""
    return tuple(
        MemoryBaseline(vendor, family, architecture, start, end, kind, reference, note, aliases)
        for start, end, kind, note in windows
    )


ARM_MEMORY_BASELINES = (
    *_family(
        "st",
        "STM32",
        "Cortex-M",
        "STM32 family memory maps",
        (
            (
                0x08000000,
                0x10000000,
                "flash",
                "Possible internal Flash and security aliases; flashless devices also exist",
            ),
            (
                0x10000000,
                0x10100000,
                "ram",
                "Possible CCM/TCM or aliases; presence and extent unknown",
            ),
            (
                0x18000000,
                0x19000000,
                "rom-or-flash",
                "Possible ROM/Flash or secure code alias on newer STM32; presence and physical extent unknown",
            ),
            (
                0x1FF00000,
                0x20000000,
                "otp",
                "System-memory/OTP candidates; not all offsets are OTP",
            ),
            (
                0x20000000,
                0x20400000,
                "ram",
                "Possible main SRAM/TCM banks; extent and holes unknown",
            ),
            (0x24000000, 0x25000000, "ram", "Possible AXI SRAM banks; extent and holes unknown"),
            (0x28000000, 0x28100000, "ram", "Possible AHB SRAM banks; extent and holes unknown"),
            (0x30000000, 0x30400000, "ram", "Possible additional SRAM or secure aliases"),
            (0x34000000, 0x35000000, "ram", "Possible secure AXI SRAM aliases, not extra capacity"),
            (0x38000000, 0x38100000, "ram", "Possible auxiliary/backup SRAM; extent unknown"),
            (
                0x40000000,
                0x60000000,
                "registers",
                "Peripheral/security-alias aperture; arbitrary reads may have side effects",
            ),
            (
                0x60000000,
                0xA0000000,
                "unknown",
                "Possible external RAM/Flash apertures; board dependent",
            ),
        ),
        ("STMicroelectronics",),
    ),
    *_family(
        "nxp",
        "LPC / Kinetis / i.MX RT",
        "Cortex-M",
        "NXP MCU family memory maps",
        (
            (
                0x00000000,
                0x02000000,
                "flash",
                "Possible internal Flash/boot aliases; some families are flashless",
            ),
            (
                0x10000000,
                0x10100000,
                "ram",
                "Possible local LPC SRAM; extent and reserved bytes unknown",
            ),
            (0x1FF00000, 0x20000000, "ram", "Possible lower SRAM banks on Kinetis devices"),
            (
                0x20000000,
                0x20400000,
                "ram",
                "Possible SRAM/TCM/AHB/OCRAM banks; holes and aliases unknown",
            ),
            (
                0x40000000,
                0x60000000,
                "registers",
                "Peripheral aperture; arbitrary reads may have side effects",
            ),
            (
                0x60000000,
                0x80000000,
                "unknown",
                "Possible external FlexSPI/RAM apertures; board dependent",
            ),
        ),
        ("NXP Semiconductors",),
    ),
    *_family(
        "nxp",
        "i.MX",
        "Cortex-A",
        "NXP application-processor memory maps",
        (
            (
                0x00900000,
                0x01000000,
                "ram",
                "Possible OCRAM banks; placement and extent vary by generation",
            ),
            (
                0x80000000,
                0x100000000,
                "ram",
                "Possible DDR aperture, not installed capacity; board dependent",
            ),
        ),
        ("NXP Semiconductors",),
    ),
    *_family(
        "infineon",
        "XMC",
        "Cortex-M",
        "Infineon XMC family memory maps",
        (
            (0x08000000, 0x0A000000, "flash", "Possible cached Flash window; extent unknown"),
            (
                0x0C000000,
                0x0E000000,
                "flash",
                "Possible uncached Flash alias, not additional capacity",
            ),
            (0x10000000, 0x10100000, "ram", "Possible PSRAM banks; extent unknown"),
            (
                0x1FF00000,
                0x20000000,
                "ram",
                "Possible additional PSRAM banks on other XMC generations",
            ),
            (0x20000000, 0x20100000, "ram", "Possible system DSRAM banks; extent unknown"),
            (0x30000000, 0x30100000, "ram", "Possible communication DSRAM banks; extent unknown"),
        ),
        ("Infineon Technologies",),
    ),
    *_family(
        "infineon",
        "PSoC / TRAVEO",
        "Cortex-M",
        "Infineon PSoC/TRAVEO family memory maps",
        (
            (0x08000000, 0x09000000, "ram", "Possible PSoC SRAM window; device dependent"),
            (0x10000000, 0x11000000, "flash", "Possible embedded Flash window; device dependent"),
            (
                0x28000000,
                0x29000000,
                "ram",
                "Possible TRAVEO SRAM banks; holes and security aliases unknown",
            ),
            (
                0x40000000,
                0x60000000,
                "registers",
                "Peripheral aperture; arbitrary reads may have side effects",
            ),
        ),
        ("Infineon Technologies",),
    ),
    *_family(
        "xilinx",
        "Zynq",
        "Cortex-A",
        "AMD Zynq family memory maps",
        (
            (
                0x00000000,
                0x80000000,
                "ram",
                "Possible DDR/boot remapping aperture; not installed RAM capacity",
            ),
            (
                0x00000000,
                0x00100000,
                "ram",
                "Possible low on-chip RAM/boot aliases; remapping and extent unknown",
            ),
            (
                0xFFFC0000,
                0x100000000,
                "ram",
                "Possible high on-chip RAM aliases; not additional capacity",
            ),
            (
                0xFC000000,
                0xFE000000,
                "flash",
                "Linear QSPI aperture: not physical Flash size or proof of configuration",
            ),
            (
                0xE0000000,
                0xF0000000,
                "registers",
                "Possible PS peripheral aperture; reserved addresses and holes included",
            ),
            (
                0xF8000000,
                0xF9000000,
                "registers",
                "Possible system-control/debug aperture; access restrictions unknown",
            ),
        ),
        ("AMD", "AMD/Xilinx"),
    ),
    *_family(
        "xilinx",
        "Zynq UltraScale+",
        "Cortex-A / Cortex-R",
        "AMD UltraScale+ memory maps",
        (
            (
                0x800000000,
                0x1000000000,
                "ram",
                "Possible high DDR aperture; 64-bit addresses, board dependent",
            ),
            (
                0xFFE00000,
                0xFFF00000,
                "ram",
                "Possible RPU TCM banks/aliases; configuration dependent",
            ),
        ),
        ("AMD", "AMD/Xilinx"),
    ),
)
