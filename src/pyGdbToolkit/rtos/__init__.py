"""RTOS implementations available to GDB commands."""

from . import camelot

SUPPORTED_RTOS = {camelot.NAME: camelot}
