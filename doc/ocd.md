# OCD Identification

The `pyGdbToolkit.ocd` module identifies the on-chip debugger connected to GDB.
It does not register a command and does not open a second probe connection.
Toolkit startup attempts a read-only probe. Loading the package without a
remote connection is supported: the identity remains `unknown`, and a later
interface call probes the connected server.

## Interface

```python
from pyGdbToolkit.ocd import OcdIdentifier, get_ocd

info = get_ocd()
if info.identifier == OcdIdentifier.OPENOCD:
    print(info.version)
```

`OcdInfo` contains:

- `identifier`: `OcdIdentifier.PYOCD`, `OPENOCD`, `JLINK`, or `UNKNOWN`, whose string
  values are `pyocd`, `openocd`, `jlink`, and `unknown`.
- `version`: the OpenOCD version banner or J-Link version (for example `9.82`),
  or `None` when unavailable. No pyOCD
  version is inferred from the target.
- `evidence`: the server response or reasons why identification failed.

`get_ocd(refresh=True)` explicitly reprobes. A successful identity is cached
for the current inferior/connection pair; switching connections invalidates
it. Disconnection clears the identity, and unknown results are retried on
subsequent calls. The interface is also exported at package level.

`probe_ocd(execute)` and `OcdDetector(execute, connection_key)` accept injected
functions for tests and other integrations. Detection uses OpenOCD's
`monitor echo [version]` banner, then pyOCD's `monitor show aps` inventory,
then the `SEGGER J-Link GDB Server V...` banner from `monitor help`.
The explicit `echo` publishes the Tcl result even on OpenOCD builds whose
target availability callbacks overwrite ordinary monitor command results.
If no recognizable response is returned, the module reports `unknown`
rather than assuming a backend. A locked or unsupported target can prevent
pyOCD's inventory response and leave detection unknown.

## J-Link Connection

SEGGER `JLinkGDBServer` and `JLinkGDBServerCLExe` require GDB's `remote` mode,
not `extended-remote`. For an already running server on the default port:

```text
target remote localhost:2331
```

Identification has been verified with J-Link V9.82 on the i.MX8MP Cortex-M7.
No AArch64 target support is added. The J-Link AP transport supports the
verified ADIv5 JTAG-DPv0 configuration, where DP SELECT can be read back.
It uses `ReadAPEx` to inspect AP registers, saves SELECT, and restores and
verifies it even when a read fails. The restoration writes only the debug-port
selection register, not AP transfer registers or target memory. SWD/other DP
versions and hardware core selection currently report explicit errors.

`dap select` changes the toolkit's profiling selection only, not the AP used
by J-Link's ordinary GDB memory accesses. The initial profiling selection is
obtained from DP SELECT. A responding AP index does not by itself prove a
distinct physical AP; aliasing is possible.

Target-memory diagnostics still require a working GDB memory view. If both
`x/1wx 0xE000ED00` and `monitor MemU32 0xE000ED00` fail, CPU identification and
security audits cannot complete. The toolkit does not silently reset the core,
clear bus faults, unlock the device or switch to another AP's address space.

The opt-in test uses an already running J-Link server on `localhost:2331`.
It requires both `PYGDB_JLINK_HARDWARE=1` and explicit risk acknowledgement
with `PYGDB_JLINK_ALLOW_INTRUSIVE=1`:

```console
PYGDB_JLINK_HARDWARE=1 PYGDB_JLINK_ALLOW_INTRUSIVE=1 .venv/bin/python -m pytest -q tests/test_jlink_hardware.py
```

It checks AP profiles, local selection, JSON reports, memory-map commands and
DP SELECT preservation on the i.MX8MP Cortex-M7. Set `PYGDB_JLINK_ENDPOINT`
to override the endpoint. It does not reset, resume or write target memory.
This does not make it harmless: AP scanning and target-memory reads may cause
bus faults or disturb debug access, and the server's connect/disconnect policy
can affect execution. On the i.MX8MP M7, diagnostic sessions have been followed
by loss of memory access and failed reconnection; the triggering operation has
not been isolated. Do not run this test on a target whose state must be preserved.

## Automatic AP Transport

`AutoDebugPortTransport` in `ap_runtime.py` consumes the detector and chooses
`PyOcdMonitorTransport`, `OpenOcdMonitorTransport` or `JLinkMonitorTransport`.
Commands do not perform
OCD detection or contain architecture-specific register decoding.

The OpenOCD adapter obtains the DAP from the currently selected target, uses
`dpreg` to determine AP addressing, `apreg` for register reads, and `apsel`
for selection. ADIv5 discovery reads the bounded APSEL range 0..255; ADIv6
discovery traverses the root ROM table through `info root`. Read failures or
unrecognized server responses are explicit errors, not empty inventories.

AP selection does not switch the GDB core or reroute ordinary GDB memory
packets. Discovery is not identical between backends: OpenOCD ROM-table
traversal can access component memory and modify MEM-AP transfer registers
as part of its own implementation. See [dap.md](dap.md) for usage and limits.
