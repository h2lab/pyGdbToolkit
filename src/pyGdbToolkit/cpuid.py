"""Decoding for the architected Arm CPUID register."""

from __future__ import annotations

from .models import CPUID

CPUID_ADDRESS = 0xE000ED00

_IMPLEMENTERS = (
    (0x41, "Arm"),
)

_CORTEX_M_PARTS = (
    (0xC20, "Cortex-M0"),
    (0xC21, "Cortex-M1"),
    (0xC23, "Cortex-M3"),
    (0xC24, "Cortex-M4"),
    (0xC27, "Cortex-M7"),
    (0xC60, "Cortex-M0+"),
    (0xD20, "Cortex-M23"),
    (0xD21, "Cortex-M33"),
    (0xD22, "Cortex-M55"),
    (0xD23, "Cortex-M85"),
    (0xD31, "Cortex-M35P"),
    (0xD32, "Cortex-M52"),
)


def decode_cpuid(raw: int) -> CPUID:
    """Decode an Arm CPUID register value.

    Parameters
    ----------
    raw
        The 32-bit value read from ``0xE000ED00``.

    Returns
    -------
    CPUID
        An immutable description of the architected CPUID fields.

    Raises
    ------
    ValueError
        If ``raw`` does not fit in an unsigned 32-bit register.
    """
    if not 0 <= raw <= 0xFFFFFFFF:
        raise ValueError("CPUID must be an unsigned 32-bit value")

    implementer = raw >> 24
    variant = (raw >> 20) & 0xF
    architecture = (raw >> 16) & 0xF
    part_number = (raw >> 4) & 0xFFF
    revision = raw & 0xF
    return CPUID(
        raw=raw,
        implementer=implementer,
        implementer_name=_implementer_name(implementer),
        variant=variant,
        architecture=architecture,
        part_number=part_number,
        revision=revision,
        core=_core_name(part_number),
    )


def _implementer_name(implementer: int) -> str:
    """Return an implementer display name.

    Parameters
    ----------
    implementer
        The CPUID implementer byte.

    Returns
    -------
    str
        The known name or a raw hexadecimal fallback.
    """
    for value, name in _IMPLEMENTERS:
        if implementer == value:
            return name
    return f"Unknown (0x{implementer:02X})"


def _core_name(part_number: int) -> str:
    """Return a Cortex-M core name from its CPUID part number.

    Parameters
    ----------
    part_number
        The CPUID part-number field.

    Returns
    -------
    str
        The known core name or a raw hexadecimal fallback.
    """
    for value, name in _CORTEX_M_PARTS:
        if part_number == value:
            return name
    return f"Unknown Cortex-M part 0x{part_number:03X}"
