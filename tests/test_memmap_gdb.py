"""Exercise metadata and CLI adapters in an actual GDB process when available."""

from pathlib import Path
import shutil
import socket
import subprocess
import sysconfig
from threading import Thread
import json

import pytest

GDB = shutil.which("gdb")
pytestmark = pytest.mark.skipif(GDB is None, reason="GDB is not installed")
ROOT = Path(__file__).resolve().parents[1]
IMPORT = (
    f"python import sys; sys.path.insert(0, {sysconfig.get_paths()['purelib']!r}); "
    f"sys.path.insert(0, {str(ROOT / 'src')!r}); import pyGdbToolkit"
)


def test_native_cli(tmp_path):
    """Discover native ELF sections, sample two points and export the report."""
    report = tmp_path / "native.json"
    commands = [
        "set pagination off",
        IMPORT,
        "file /bin/true",
        "starti",
        "memmap discover",
        'python addr = int(gdb.parse_and_eval("$pc")) & ~3; '
        'gdb.execute(f"memmap probe --range {addr}:{addr+8} --allow-unsafe --stride 4")',
        f"memmap report {report}",
    ]
    arguments = [str(GDB), "--nx", "--batch"]
    for command in commands:
        arguments.extend(["-ex", command])
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
    if "Operation not permitted" in result.stderr:
        pytest.skip("native GDB ptrace is unavailable")
    assert report.exists(), result.stdout + result.stderr
    data = json.loads(report.read_text())
    assert data["architecture"]
    assert {hint["name"] for hint in data["execution_hints"]} >= {"PC", "SP"}
    assert any(region["source"] == "elf" for region in data["regions"])
    assert [sample["status"] for sample in data["observations"]] == ["readable-sampled"] * 2
    assert data["probe_runs"][0]["stop_reason"] == "complete"


def test_real_qxfer_output():
    """Parse the actual maintenance-packet display, not only fabricated output."""
    xml = b'<?xml version="1.0"?>\n<!DOCTYPE memory-map SYSTEM "gdb-memory-map.dtd">\n<memory-map><memory type="ram" start="0x20000000" length="0x1000"/></memory-map>'
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(15)
    port = listener.getsockname()[1]

    def serve():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(15)
                stream = connection.makefile("rb")
                while True:
                    marker = stream.read(1)
                    if not marker:
                        return
                    if marker != b"$":
                        continue
                    packet = bytearray()
                    while True:
                        byte = stream.read(1)
                        if not byte:
                            return
                        if byte == b"#":
                            break
                        packet.extend(byte)
                    stream.read(2)
                    request = bytes(packet)
                    response = b""
                    if request.startswith(b"qSupported"):
                        response = b"PacketSize=4000;qXfer:memory-map:read+"
                    elif request.startswith(b"qXfer:memory-map:read::"):
                        offset, length = request.rsplit(b":", 1)[1].split(b",")
                        start, size = int(offset, 16), int(length, 16)
                        chunk = xml[start : start + size]
                        response = (b"l" if start + size >= len(xml) else b"m") + chunk
                    elif request == b"?":
                        response = b"S05"
                    elif request == b"qfThreadInfo":
                        response = b"m1"
                    elif request == b"qsThreadInfo":
                        response = b"l"
                    elif request == b"qC":
                        response = b"QC1"
                    elif request.startswith((b"H", b"T")):
                        response = b"OK"
                    elif request == b"g":
                        response = b"0" * 624
                    elif request.startswith(b"m"):
                        response = b"E01"
                    elif request.startswith(b"D"):
                        response = b"OK"
                    checksum = f"{sum(response) % 256:02x}".encode()
                    connection.sendall(b"+$" + response + b"#" + checksum)
        except (OSError, TimeoutError):
            return

    thread = Thread(target=serve, daemon=True)
    thread.start()
    commands = [
        "set pagination off",
        "set architecture i386",
        f"target remote 127.0.0.1:{port}",
        IMPORT,
        "maintenance packet qXfer:memory-map:read::0,1000",
        "python from pyGdbToolkit.memmap_runtime import remote_memory_map; "
        "regions = remote_memory_map(lambda command: gdb.execute(command, to_string=True)); "
        "print('MEMMAP_RANGE', hex(regions[0].start), hex(regions[0].end))",
    ]
    arguments = [str(GDB), "--nx", "--batch"]
    for command in commands:
        arguments.extend(["-ex", command])
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=25)
    finally:
        listener.close()
        thread.join(timeout=2)
    assert "MEMMAP_RANGE 0x20000000 0x20001000" in result.stdout, result.stdout + result.stderr
