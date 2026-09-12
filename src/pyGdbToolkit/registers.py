"""Typed CPU register access and unified multi-architecture support via GDB Python API.

Provides an extensible architecture hierarchy for microcontroller targets
(ARM, AVR, PowerPC MPC, etc.) selected automatically via GDB's architecture
introspection, with full implementation for ARM Cortex-M architectures.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Mapping, Protocol, Sequence

import gdb


class RegisterError(RuntimeError):
    """Base exception for register access errors."""


class RegisterReadError(RegisterError):
    """Raised when reading a CPU register fails."""

    def __init__(self, register: str, reason: str) -> None:
        self.register = register
        self.reason = reason
        super().__init__(f"could not read register '{register}': {reason}")


class RegisterWriteError(RegisterError):
    """Raised when writing to a CPU register fails."""

    def __init__(self, register: str, value: int, reason: str) -> None:
        self.register = register
        self.value = value
        self.reason = reason
        super().__init__(f"could not write 0x{value:X} to register '{register}': {reason}")


class RegisterNotFoundError(RegisterError):
    """Raised when a requested register does not exist in the target architecture."""

    def __init__(self, register: str) -> None:
        self.register = register
        super().__init__(f"register '{register}' not found in current architecture")


class UnsupportedArchitectureError(RegisterError):
    """Raised when target architecture is not recognized or unsupported."""

    def __init__(self, arch_name: str) -> None:
        self.arch_name = arch_name
        super().__init__(f"architecture '{arch_name}' is not supported by register provider")


class RegisterAccessor(Protocol):
    """Protocol defining unified register manipulation operations across architectures."""

    @property
    def architecture_name(self) -> str:
        """Name of the target architecture."""
        ...

    def read(self, name: str) -> int:
        """Read an unsigned integer value from a CPU register."""
        ...

    def read_optional(self, name: str, default: int | None = None) -> int | None:
        """Read a register value or return default if unavailable."""
        ...

    def write(self, name: str, value: int) -> None:
        """Write an integer value to a CPU register."""
        ...

    def read_multiple(self, names: Sequence[str]) -> dict[str, int]:
        """Read multiple registers in batch."""
        ...

    def has_register(self, name: str) -> bool:
        """Check if a register exists and is accessible."""
        ...

    def read_bitfield(self, name: str, shift: int, width: int) -> int:
        """Extract a bitfield from a register."""
        ...

    def read_bit(self, name: str, bit: int) -> bool:
        """Test a single bit in a register."""
        ...

    def write_bitfield(self, name: str, shift: int, width: int, value: int) -> None:
        """Modify a bitfield in a register (read-modify-write)."""
        ...

    def write_bit(self, name: str, bit: int, value: bool) -> None:
        """Set or clear a single bit in a register."""
        ...

    def pc(self) -> int:
        """Return the current Program Counter value."""
        ...

    def sp(self) -> int:
        """Return the active Stack Pointer value."""
        ...

    def lr(self) -> int | None:
        """Return the Link Register value if available on this architecture."""
        ...

    def flags(self) -> int | None:
        """Return the processor status / flags register value."""
        ...

    def general_registers(self) -> dict[str, int]:
        """Return a mapping of standard general-purpose registers."""
        ...


class BaseRegisterAccessor(ABC, RegisterAccessor):
    """Abstract base class implementing generic GDB register access and bit manipulation.

    Subclasses provide architecture-specific aliases, register sets, and status decoding.
    """

    def __init__(
        self,
        frame: gdb.Frame | None = None,
        bit_mask: int = 0xFFFFFFFF,
        arch_name: str = "generic",
    ) -> None:
        self._frame = frame
        self._bit_mask = bit_mask
        self._arch_name = arch_name

    @property
    def architecture_name(self) -> str:
        return self._arch_name

    def _get_frame(self) -> gdb.Frame:
        """Retrieve the target frame or current selected frame."""
        if self._frame is not None:
            return self._frame
        try:
            return gdb.selected_frame()
        except gdb.error as error:
            raise RegisterReadError("*frame*", f"could not obtain selected frame: {error}") from error

    def _normalize_name(self, name: str) -> str:
        """Normalize register name and apply architecture-specific alias translation."""
        clean = name.strip()
        if clean.startswith("$"):
            clean = clean[1:]
        clean = clean.lower()
        return self.resolve_alias(clean)

    def resolve_alias(self, name: str) -> str:
        """Hook for architecture subclasses to translate aliases (e.g. r15 -> pc)."""
        return name

    def read(self, name: str) -> int:
        norm_name = self._normalize_name(name)

        # 1. Native frame API
        try:
            frame = self._get_frame()
            val = frame.read_register(norm_name)
            return int(val) & self._bit_mask
        except Exception:
            pass

        # 2. GDB expression evaluator fallback ($register)
        try:
            val = gdb.parse_and_eval(f"${norm_name}")
            return int(val) & self._bit_mask
        except Exception as error:
            raise RegisterReadError(name, str(error)) from error

    def read_optional(self, name: str, default: int | None = None) -> int | None:
        try:
            return self.read(name)
        except RegisterError:
            return default

    def write(self, name: str, value: int) -> None:
        norm_name = self._normalize_name(name)
        masked_value = value & self._bit_mask

        # 1. Assign to Value object if supported
        try:
            frame = self._get_frame()
            reg_val = frame.read_register(norm_name)
            reg_val.assign(gdb.Value(masked_value))
            return
        except Exception:
            pass

        # 2. Set through GDB command execution
        try:
            gdb.execute(f"set ${norm_name} = {masked_value:#x}", to_string=True)
        except Exception as error:
            raise RegisterWriteError(name, value, str(error)) from error

    def read_multiple(self, names: Sequence[str]) -> dict[str, int]:
        results: dict[str, int] = {}
        for name in names:
            results[name] = self.read(name)
        return results

    def has_register(self, name: str) -> bool:
        return self.read_optional(name) is not None

    def read_bitfield(self, name: str, shift: int, width: int) -> int:
        if shift < 0 or width <= 0:
            raise ValueError("shift must be >= 0 and width must be > 0")
        val = self.read(name)
        mask = (1 << width) - 1
        return (val >> shift) & mask

    def read_bit(self, name: str, bit: int) -> bool:
        return bool(self.read_bitfield(name, shift=bit, width=1))

    def write_bitfield(self, name: str, shift: int, width: int, value: int) -> None:
        if shift < 0 or width <= 0:
            raise ValueError("shift must be >= 0 and width must be > 0")
        mask = (1 << width) - 1
        current = self.read(name)
        updated = (current & ~(mask << shift)) | ((value & mask) << shift)
        self.write(name, updated)

    def write_bit(self, name: str, bit: int, value: bool) -> None:
        self.write_bitfield(name, shift=bit, width=1, value=1 if value else 0)

    @abstractmethod
    def pc(self) -> int:
        """Return the current Program Counter."""
        ...

    @abstractmethod
    def sp(self) -> int:
        """Return the active Stack Pointer."""
        ...

    @abstractmethod
    def lr(self) -> int | None:
        """Return the Link Register."""
        ...

    @abstractmethod
    def flags(self) -> int | None:
        """Return the Status / Flags register."""
        ...

    @abstractmethod
    def general_registers(self) -> dict[str, int]:
        """Return standard general-purpose registers."""
        ...


class GenericRegisterAccessor(BaseRegisterAccessor):
    """Fallback register accessor for unknown architectures."""

    def pc(self) -> int:
        return self.read("pc")

    def sp(self) -> int:
        return self.read("sp")

    def lr(self) -> int | None:
        return self.read_optional("lr")

    def flags(self) -> int | None:
        return self.read_optional("flags")

    def general_registers(self) -> dict[str, int]:
        return {}


ArchitectureMatcher = Callable[[str], bool]
RegisterAccessorFactory = Callable[[gdb.Frame | None, str], BaseRegisterAccessor]


class ArchitectureRegistry:
    """Registry matching GDB architecture names to specialized RegisterAccessor classes."""

    def __init__(self) -> None:
        # Deferred import: arch modules import BaseRegisterAccessor from this module.
        from .arch.registers_arm import ArmRegisterAccessor
        from .arch.registers_avr import AvrRegisterAccessor
        from .arch.registers_ppc import PowerPcRegisterAccessor

        self._ArmRegisterAccessor = ArmRegisterAccessor
        self._AvrRegisterAccessor = AvrRegisterAccessor
        self._PowerPcRegisterAccessor = PowerPcRegisterAccessor
        self._providers: list[tuple[ArchitectureMatcher, RegisterAccessorFactory]] = []
        self._register_defaults()

    def _register_defaults(self) -> None:
        """Register built-in architectures."""
        self.register(
            lambda arch: any(k in arch for k in ("arm", "cortex-m", "thumb")),
            lambda frame, arch: self._ArmRegisterAccessor(frame=frame, arch_name=arch),
        )
        self.register(
            lambda arch: "avr" in arch,
            lambda frame, arch: self._AvrRegisterAccessor(frame=frame, arch_name=arch),
        )
        self.register(
            lambda arch: any(k in arch for k in ("powerpc", "ppc", "mpc")),
            lambda frame, arch: self._PowerPcRegisterAccessor(frame=frame, arch_name=arch),
        )

    def register(
        self,
        matcher: ArchitectureMatcher,
        factory: RegisterAccessorFactory,
    ) -> None:
        """Register a new architecture accessor factory."""
        self._providers.append((matcher, factory))

    def detect_architecture_name(self, frame: gdb.Frame | None = None) -> str:
        """Query GDB Python API to determine the current target architecture name."""
        # 1. Try from frame
        if frame is not None:
            try:
                return frame.architecture().name().lower()
            except (gdb.error, AttributeError):
                pass

        try:
            return gdb.selected_frame().architecture().name().lower()
        except (gdb.error, AttributeError):
            pass

        # 2. Try from selected inferior
        try:
            return gdb.selected_inferior().architecture().name().lower()
        except (gdb.error, AttributeError):
            pass

        return "unknown"

    def get_accessor(
        self,
        frame: gdb.Frame | None = None,
        arch_name: str | None = None,
    ) -> BaseRegisterAccessor:
        """Select and return the appropriate RegisterAccessor instance.

        Parameters
        ----------
        frame
            Optional GDB frame.
        arch_name
            Optional explicit architecture name. If None, auto-detected from GDB API.

        Returns
        -------
        BaseRegisterAccessor
            Specialized accessor instance for the target architecture.
        """
        detected_arch = (arch_name or self.detect_architecture_name(frame)).lower()

        for matcher, factory in self._providers:
            if matcher(detected_arch):
                return factory(frame, detected_arch)

        # Fallback to Generic
        return GenericRegisterAccessor(frame=frame, arch_name=detected_arch)


DEFAULT_ARCHITECTURE_REGISTRY = ArchitectureRegistry()


def get_register_accessor(
    frame: gdb.Frame | None = None,
    arch_name: str | None = None,
) -> BaseRegisterAccessor:
    """Obtain a specialized register accessor for the current GDB target."""
    return DEFAULT_ARCHITECTURE_REGISTRY.get_accessor(frame=frame, arch_name=arch_name)


def read_register(name: str, frame: gdb.Frame | None = None) -> int:
    """Read a register value using the auto-detected architecture accessor."""
    return get_register_accessor(frame=frame).read(name)


def try_read_register(name: str, default: int | None = None, frame: gdb.Frame | None = None) -> int | None:
    """Read a register value or return default if unavailable."""
    return get_register_accessor(frame=frame).read_optional(name, default=default)


def write_register(name: str, value: int, frame: gdb.Frame | None = None) -> None:
    """Write an integer to a register."""
    get_register_accessor(frame=frame).write(name, value)


def read_bitfield(name: str, shift: int, width: int, frame: gdb.Frame | None = None) -> int:
    """Extract a bitfield from a register."""
    return get_register_accessor(frame=frame).read_bitfield(name, shift, width)


def read_bit(name: str, bit: int, frame: gdb.Frame | None = None) -> bool:
    """Read a single bit from a register."""
    return get_register_accessor(frame=frame).read_bit(name, bit)
