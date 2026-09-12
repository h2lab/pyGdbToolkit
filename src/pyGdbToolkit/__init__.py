"""GDB commands provided by pyGdbToolkit."""

from .cmd_faultinfo import FaultInfoCmd
from .cmd_lscpu import LscpuCmd

LscpuCmd()
FaultInfoCmd()

__all__ = ["FaultInfoCmd", "LscpuCmd"]
