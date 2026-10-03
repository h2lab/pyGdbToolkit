"""Architecture-neutral debug-port contracts and OCD monitor transports."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable, Protocol


class DebugPortError(RuntimeError):
    """A debug server cannot complete a debug-port operation."""


@dataclass(frozen=True)
class AccessPort:
    """One access port discovered by the debug server."""

    index: int
    name: str
    selected: bool = False
    ap_version: int = 1


class DebugPortTransport(Protocol):
    """Debug-port operations independent of CPU architecture and wire protocol."""

    def list_access_ports(self) -> tuple[AccessPort, ...]:
        """Return the ports discovered by the server."""
        ...

    def select_access_port(self, index: int) -> None:
        """Select a port in the debug server."""
        ...

    def read_ap(self, index: int, address: int) -> int:
        """Read an access-port register, not target memory."""
        ...


class PyOcdMonitorTransport:
    """Use pyOCD's public monitor commands over GDB's remote connection.

    This backend supports APv1 (ADIv5) and APv2 (ADIv6). It does not assume
    whether the probe uses JTAG or SWD.
    """

    def __init__(self, execute: Callable[[str], str]) -> None:
        """Inject the GDB monitor executor."""
        self._execute = execute
        self._ports: dict[int, AccessPort] = {}

    def _monitor(self, command: str) -> str:
        try:
            output = self._execute(f"monitor {command}")
        except Exception as error:
            raise DebugPortError(f"pyOCD monitor: {error}") from error
        if re.search(r"error|unknown command|invalid command|target is locked", output, re.I):
            raise DebugPortError(output.strip())
        return output

    def list_access_ports(self) -> tuple[AccessPort, ...]:
        """Read the server's discovered ports without scanning arbitrary addresses."""
        output = self._monitor("show aps")
        count = re.search(r"^\s*(\d+) APs:\s*$", output, re.M)
        if count is None:
            raise DebugPortError("Expected pyOCD 'show aps' output; backend unsupported")
        ports = tuple(
            AccessPort(
                int(match[1][1:], 16) if match[1].startswith("@") else int(match[1]),
                match[2].strip(),
                bool(match[3]),
                2 if match[1].startswith("@") else 1,
            )
            for match in re.finditer(
                r"^\s*(@0x[0-9a-fA-F]+|\d+):\s*(.+?)(\s+\(selected\))?\s*$", output, re.M
            )
        )
        if len(ports) != int(count[1]):
            raise DebugPortError("Unsupported AP addressing or incomplete pyOCD AP list")
        self._ports = {port.index: port for port in ports}
        return ports

    def select_access_port(self, index: int) -> None:
        """Change pyOCD's MEM-AP selection and verify the resulting state."""
        if index not in self._ports:
            self.list_access_ports()
        if index not in self._ports:
            raise DebugPortError(f"AP {index} was not discovered")
        self._monitor(f"set mem-ap {index:#x}")
        if not any(port.index == index and port.selected for port in self.list_access_ports()):
            raise DebugPortError(f"pyOCD did not select AP {index}")

    def read_ap(self, index: int, address: int) -> int:
        """Read a register using the addressing version reported by pyOCD."""
        port = self._ports.get(index)
        if port is not None and port.ap_version == 2:
            if index % 4096 or not 0 <= address <= 0xFFC or address % 4:
                raise DebugPortError("Invalid APv2 register address or unaligned AP base")
            expected_address = index + address
            command = f"readap 0x{expected_address:x}"
        else:
            self._check_index(index)
            if not 0 <= address <= 0xFC or address % 4:
                raise DebugPortError("APv1 register address must be aligned and within 0x00..0xfc")
            expected_address = (index << 24) | address
            command = f"readap {index} 0x{address:x}"
        output = self._monitor(command)
        match = re.search(r"AP register 0x([0-9a-f]+) = 0x([0-9a-f]{1,8})\b", output, re.I)
        if match is None or int(match[1], 16) != expected_address:
            raise DebugPortError(f"Invalid pyOCD AP register response: {output.strip()}")
        return int(match[2], 16)

    @staticmethod
    def _check_index(index: int) -> None:
        if not 0 <= index <= 255:
            raise DebugPortError("This backend only supports APv1 indices 0..255")


class OpenOcdMonitorTransport:
    """Access the current target's DAP through OpenOCD's public Tcl commands."""

    def __init__(self, execute: Callable[[str], str]) -> None:
        """Inject monitor execution without opening another probe connection."""
        self._execute = execute
        self._dap = ""
        self._ap_version = 1
        self._ports: dict[int, AccessPort] = {}

    @property
    def discovery(self) -> str:
        """Describe the inventory source without promising hidden-port discovery."""
        return "OpenOCD ADIv6 root ROM table" if self._ap_version == 2 else "OpenOCD ADIv5 IDR scan"

    def _monitor(self, command: str) -> str:
        try:
            output = self._execute(f"monitor {command}")
        except Exception as error:
            raise DebugPortError(f"OpenOCD monitor: {error}") from error
        if re.search(
            r"^\s*(?:Error:|invalid command|Unknown command|Cannot get AP)", output, re.I | re.M
        ):
            raise DebugPortError(output.strip())
        return output

    def _value(self, command: str) -> int:
        output = self._monitor(f"echo [{command}]")
        values = re.findall(r"^\s*0x([0-9a-fA-F]+)\s*$", output, re.M)
        if len(values) != 1:
            raise DebugPortError(f"Invalid OpenOCD register response: {output.strip()}")
        return int(values[0], 16)

    def _context(self) -> None:
        output = self._monitor("echo [[target current] cget -dap]")
        names = [
            line.strip()
            for line in output.splitlines()
            if line.strip() not in ("available", "unavailable", "")
        ]
        if len(names) != 1 or re.fullmatch(r"[A-Za-z0-9_.:-]+", names[0]) is None:
            raise DebugPortError("The current OpenOCD target does not expose a usable DAP")
        dap = names[0]
        if dap != self._dap:
            self._ports.clear()
        self._dap = dap
        dpidr = self._value(f"{dap} dpreg 0")
        self._ap_version = 2 if ((dpidr >> 12) & 0xF) >= 3 else 1

    def list_access_ports(self) -> tuple[AccessPort, ...]:
        """Discover ADIv6 ROM-table ports or read the bounded ADIv5 AP inventory."""
        self._context()
        selected = self._value(f"{self._dap} apsel")
        ports: list[AccessPort] = []
        if self._ap_version == 2:
            output = self._monitor(f"echo [{self._dap} info root]")
            blocks = re.split(r"^\s*AP # 0x([0-9a-fA-F]+)\s*$", output, flags=re.M)
            if len(blocks) < 3:
                raise DebugPortError("OpenOCD did not return an ADIv6 root ROM table")
            for position in range(1, len(blocks), 2):
                index = int(blocks[position], 16)
                body = blocks[position + 1]
                idr = re.search(r"AP ID register 0x([0-9a-fA-F]+)", body)
                if idr is None and "Access Port" not in body:
                    continue
                if idr is not None and int(idr[1], 16) in (0, 0xFFFFFFFF):
                    continue
                kind = re.search(
                    r"^\s*Type is (MEM-AP[^\r\n]*|JTAG-AP[^\r\n]*|COM-AP[^\r\n]*)", body, re.M
                )
                ports.append(
                    AccessPort(
                        index, kind[1].strip() if kind else "Access Port", index == selected, 2
                    )
                )
        else:
            script = (
                "for {set pygdb_ap 0} {$pygdb_ap < 256} {incr pygdb_ap} {"
                f"if {{[catch {{{self._dap} apid $pygdb_ap}} pygdb_idr]}} {{"
                "echo [format {PYGDB_AP_ERROR %d} $pygdb_ap]"
                "} elseif {$pygdb_idr != 0 && $pygdb_idr != 0xffffffff} {"
                "echo [format {PYGDB_AP %d 0x%08x} $pygdb_ap $pygdb_idr]}}; echo PYGDB_AP_END"
            )
            output = self._monitor(script)
            if not re.search(r"^PYGDB_AP_END\s*$", output, re.M):
                raise DebugPortError("OpenOCD did not complete the ADIv5 AP scan")
            if "PYGDB_AP_ERROR" in output:
                raise DebugPortError(
                    "OpenOCD AP discovery is incomplete: one or more IDR reads failed"
                )
            for match in re.finditer(r"^PYGDB_AP (\d+) 0x[0-9a-fA-F]+\s*$", output, re.M):
                index = int(match[1])
                ports.append(AccessPort(index, "Access Port", index == selected))
        self._ports = {port.index: port for port in ports}
        return tuple(self._ports.values())

    def select_access_port(self, index: int) -> None:
        """Change the DAP's AP selection without changing the GDB core."""
        if not self._ports:
            self.list_access_ports()
        if index not in self._ports:
            raise DebugPortError(f"AP {index} was not discovered")
        self._monitor(f"{self._dap} apsel {index:#x}")
        if self._value(f"{self._dap} apsel") != index:
            raise DebugPortError(f"OpenOCD did not select AP {index}")

    def read_ap(self, index: int, address: int) -> int:
        """Read an AP register with explicit port and version-correct offset."""
        if not self._dap:
            self._context()
        maximum = 0xFFC if self._ap_version == 2 else 0xFC
        valid_index = (
            0 <= index < (1 << 64) and index % 4096 == 0
            if self._ap_version == 2
            else 0 <= index <= 255
        )
        if not valid_index or not 0 <= address <= maximum or address % 4:
            raise DebugPortError("Invalid OpenOCD AP identifier or register offset")
        value = self._value(f"{self._dap} apreg {index:#x} {address:#x}")
        if value > 0xFFFFFFFF:
            raise DebugPortError("OpenOCD AP register value exceeds 32 bits")
        return value
