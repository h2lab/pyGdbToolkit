# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Immutable models used by CPU decoders and target providers."""

from __future__ import annotations

from dataclasses import dataclass

from .coresight import CoreSightDiscovery


@dataclass(frozen=True)
class CPUID:
    """Fields decoded from the Arm CPUID register."""

    raw: int
    implementer: int
    implementer_name: str
    variant: int
    architecture: int
    part_number: int
    revision: int
    core: str

    @property
    def rnp_revision(self) -> str:
        """Return the standard Arm rNp CPU-revision notation."""
        return f"r{self.variant}p{self.revision}"


@dataclass(frozen=True)
class FieldValue:
    """A report field that is either known or explicitly unavailable."""

    value: str | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        """Ensure that a field has exactly one state."""
        if (self.value is None) == (self.unavailable_reason is None):
            raise ValueError("a field must have either a value or an unavailable reason")

    @classmethod
    def known(cls, value: str) -> FieldValue:
        """Create a known report field.

        Parameters
        ----------
        value
            The text to display.

        Returns
        -------
        FieldValue
            A field containing ``value``.
        """
        return cls(value=value)

    @classmethod
    def unavailable(cls, reason: str) -> FieldValue:
        """Create an unavailable report field.

        Parameters
        ----------
        reason
            The reason no trustworthy value is available.

        Returns
        -------
        FieldValue
            A field with an explicit unavailable-state reason.
        """
        return cls(unavailable_reason=reason)

    @property
    def is_available(self) -> bool:
        """Whether this field holds a known value."""
        return self.value is not None

    def display(self) -> str:
        """Return the report-ready field text."""
        if self.value is not None:
            return self.value
        return f"Unavailable: {self.unavailable_reason}"


@dataclass(frozen=True)
class DeviceReport:
    """CPU and optional vendor device information for the ``lscpu`` report."""

    cpuid: CPUID
    discovery: CoreSightDiscovery
    vendor: str
    product_line: FieldValue
    part_number: FieldValue
    ram: FieldValue
    flash: FieldValue
    package: FieldValue
    serial_number: FieldValue
