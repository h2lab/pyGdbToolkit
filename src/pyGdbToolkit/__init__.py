# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""GDB commands provided by pyGdbToolkit."""

from .cmd_faultinfo import FaultInfoCmd
from .cmd_lscpu import LscpuCmd
from . import cmd_rtos
from .cmd_rtos import RtosCmd
from . import cmd_secscan
from .cmd_secscan import SecscanCmd
from . import cmd_svd
from .cmd_svd import SvdCmd

LscpuCmd()
FaultInfoCmd()
_RTOS_COMMAND = RtosCmd()
cmd_rtos.RtosSelectCmd(_RTOS_COMMAND)
cmd_rtos.RtosListCmd(_RTOS_COMMAND)
cmd_rtos.RtosLoadProjectCmd(_RTOS_COMMAND)
cmd_rtos.RtosShowCmd(_RTOS_COMMAND)
_SVD_COMMAND = SvdCmd()
cmd_svd.SvdLoadCmd(_SVD_COMMAND)
cmd_svd.SvdReadCmd(_SVD_COMMAND)
cmd_svd.SvdShowCmd(_SVD_COMMAND)
cmd_svd.SvdWriteCmd(_SVD_COMMAND)
cmd_svd.SvdMonitorCmd(_SVD_COMMAND)
cmd_svd.SvdDumpCmd(_SVD_COMMAND)
cmd_svd.SvdListCmd(_SVD_COMMAND)
cmd_svd.SvdHelpCmd(_SVD_COMMAND)

_SECSCAN_COMMAND = SecscanCmd()
cmd_secscan.SecscanAuditCmd(_SECSCAN_COMMAND)
cmd_secscan.SecscanReportCmd(_SECSCAN_COMMAND)
cmd_secscan.SecscanHelpCmd(_SECSCAN_COMMAND)

__all__ = [
    "FaultInfoCmd",
    "LscpuCmd",
    "RtosCmd",
    "SecscanCmd",
    "SvdCmd",
    "cmd_rtos",
    "cmd_secscan",
    "cmd_svd",
]
