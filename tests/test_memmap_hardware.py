"""Opt-in discovery validation against an already running OCD and STM32 target."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sysconfig

import pytest


@pytest.mark.skipif(
    os.environ.get("PYGDB_MEMMAP_HARDWARE") != "1", reason="requires an existing OCD and board"
)
def test_confirmed_session_discovery_on_hardware(tmp_path):
    """Discover without SVD/ELF and verify SESSION, rendering and policy restoration."""
    debugger = shutil.which("gdb-multiarch")
    if debugger is None:
        pytest.skip("gdb-multiarch is not installed")
    endpoint = os.environ.get("PYGDB_MEMMAP_ENDPOINT", "localhost:3333")
    assert re.fullmatch(r"[A-Za-z0-9_.:\[\]-]+", endpoint), "invalid GDB endpoint"
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "mapping.json"
    setup = (
        f"python import sys; sys.path.insert(0, {sysconfig.get_paths()['purelib']!r}); "
        f"sys.path.insert(0, {str(root / 'src')!r}); import pyGdbToolkit"
    )
    commands = [
        "set pagination off",
        "set remotetimeout 3",
        setup,
        f"target extended-remote {endpoint}",
        "python original_policy = gdb.parameter('mem inaccessible-by-default')",
        "memmap discover --verify",
        "python assert pyGdbToolkit.SESSION.target_info is not None; "
        "assert pyGdbToolkit.SESSION.target_info.confirmed; "
        "assert pyGdbToolkit.SESSION.memory_regions; "
        "assert gdb.parameter('mem inaccessible-by-default') == original_policy",
        f"memmap report {output}",
        "disconnect",
    ]
    arguments = [debugger, "--nx", "--batch"]
    for command in commands:
        arguments.extend(["-ex", command])
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert report["fingerprint"]["confidence"] == "hardware-confirmed"
    assert report["context"]["objfiles"] == []
    assert report["regions"] and report["candidates"] and report["observations"]
    assert {hint["name"] for hint in report["execution_hints"]} >= {"PC", "SP", "VTOR"}
    assert not any(region["source"] == "device-documentation" for region in report["regions"])
    assert not any(region["source"] == "svd" for region in report["regions"])
    rendered = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
    assert "SoC/family:" in rendered and "Observed points" in rendered
