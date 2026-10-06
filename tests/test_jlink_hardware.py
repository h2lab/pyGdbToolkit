"""Potentially intrusive J-Link validation requiring explicit risk acknowledgement."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sysconfig

import pytest


@pytest.mark.skipif(
    os.environ.get("PYGDB_JLINK_HARDWARE") != "1"
    or os.environ.get("PYGDB_JLINK_ALLOW_INTRUSIVE") != "1",
    reason="requires J-Link hardware and explicit acknowledgement of intrusive AP/memory probes",
)
def test_jlink_dap_commands_preserve_probe_selection(tmp_path: Path) -> None:
    """Run real DAP and memory-map commands without reset or target-memory writes."""
    debugger = shutil.which("gdb-multiarch")
    if debugger is None:
        pytest.skip("gdb-multiarch is not installed")
    endpoint = os.environ.get("PYGDB_JLINK_ENDPOINT", "localhost:2331")
    assert re.fullmatch(r"[A-Za-z0-9_.:\[\]-]+", endpoint), "invalid GDB endpoint"
    root = Path(__file__).resolve().parents[1]
    dap_report = tmp_path / "dap.json"
    memmap_report = tmp_path / "memmap.json"
    setup = (
        f"python import sys; sys.path.insert(0, {sysconfig.get_paths()['purelib']!r}); "
        f"sys.path.insert(0, {str(root / 'src')!r}); import pyGdbToolkit; "
        "from pyGdbToolkit.ocd import get_ocd, OcdIdentifier; "
        "assert get_ocd().identifier == OcdIdentifier.JLINK; "
        "assert gdb.selected_inferior().connection.type == 'remote'; "
        "original_select = gdb.execute('monitor ReadDP 2', to_string=True)"
    )
    commands = [
        "set pagination off",
        "set architecture arm",
        "set tcp connect-timeout 5",
        "set remotetimeout 5",
        f"target remote {endpoint}",
        setup,
        "dap list",
        "dap profile 4",
        "dap select 1",
        "dap profile",
        f"dap report {json.dumps(str(dap_report))}",
        "memmap discover",
        "memmap show --brief",
        "memmap probe --known --max-reads 4",
        f"memmap report {json.dumps(str(memmap_report))}",
        "rtos list",
        "svd help",
        "python assert gdb.execute('monitor ReadDP 2', to_string=True) == original_select",
        "disconnect",
    ]
    arguments = [debugger, "--nx", "--batch"]
    for command in commands:
        arguments.extend(["-ex", command])
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(dap_report.read_text())
    assert report["backend"] == "JLinkMonitorTransport"
    assert report["selected_ap"] == 1
    ports = {port["index"]: port for port in report["access_ports"]}
    assert ports[4]["registers"]["IDR"] == 0x64770001
    assert ports[4]["capabilities"]["rom_table_address"] == 0xE00FE000
    assert ports[1]["type_name"] == "APB-AP"
    assert memmap_report.is_file()
