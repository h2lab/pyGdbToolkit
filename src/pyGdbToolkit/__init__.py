"""GDB commands provided by pyGdbToolkit."""

from .cmd_lscpu import LscpuCmd
from .cmd_faultinfo import FaultInfoCommand

LscpuCmd()
FaultInfoCommand()

__all__ = ["LscpuCmd"]
