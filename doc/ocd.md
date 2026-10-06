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
This adds server identification, not AArch64 target support. Ordinary GDB
memory and register access remain available. J-Link DAP transport and hardware
core selection are not implemented; those operations report explicit errors
instead of falling back to another server's monitor commands.

## Automatic AP Transport

`AutoDebugPortTransport` in `ap_runtime.py` consumes the detector and chooses
`PyOcdMonitorTransport` or `OpenOcdMonitorTransport`. Commands do not perform
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
