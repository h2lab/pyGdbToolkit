<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# OCD Backends and Identification

The `pyGdbToolkit.ocd` package identifies the on-chip debugger connected to GDB
and owns the debug-server-specific CPU selection strategies.
It does not register a command and does not open a second probe connection.
Toolkit startup attempts a read-only probe. Loading the package without a
remote connection is supported: the identity remains `unknown`, and a later
interface call probes the connected server.

## Package Hierarchy

```text
pyGdbToolkit/
  core_runtime.py               CLI/RPC orchestration and cache invalidation
  ocd/
    __init__.py                 Stable public detection API
    detection.py                Connection-aware OCD identification
    base.py                     CoreInfo, attachment state and CoreBackend contract
    context.py                  Shared GDB inferior lifecycle and rollback
    registry.py                 Resolution of detected server strategies
    jlinkgdbserver.py            Attached-core metadata, configured endpoints and identity
    openocd.py                  Named hardware-thread inventory and selection
    pyocd.py                    Server inventory and per-core endpoint mapping
```

`CoreBackend` defines the same external CPU interface for all three strategies:
`list_cores()`, `current_core()`, and `select_core(core)`. Registration methods
are explicit capabilities: a backend that does not accept configured cluster
or attached-core metadata rejects them rather than inventing an inventory.
`invalidates_selection` lets an attached-core no-op preserve cached inspection,
while context-changing selection attempts invalidate it.

`GdbCoreController` resolves the backend through `CoreBackendRegistry`, validates
the requested ID, delegates selection, verifies the selected ID and invalidates
the shared toolkit session. It contains no server-specific inventory parser,
monitor command, affinity interpretation or selection branch. Existing
`CORES.register_jlink_core()` and `CORES.register_jlink_cluster()` entry points
remain thin compatibility delegations for pyGdbServer.

Each backend receives `CoreContextAccess`, a protocol exposing the session,
current remote connection, valid attachment lookup and `select_endpoint()`.
`GdbCoreContext` implements those common GDB operations: create an inferior,
copy architecture and symbols, connect, reuse verified attachments, and restore
the prior context if a new attachment fails. Backends supply their connection
mode and optional verification callback; this shared layer does not identify
the server or decode architecture registers.

Backend-specific state lives alongside its strategy. The shared `CoreInfo`
and `CoreSessionState` models live in `ocd/base.py`; J-Link connection and cluster
states live in `ocd/jlinkgdbserver.py`. Their historical imports from
`core_runtime.py` remain aliases to the same classes, not duplicate state types.
The former `core_jlink.py` helper has been merged into the J-Link backend.

The package root exports detection only, keeping session initialization free of
eager backend imports and circular dependencies. Backend strategies are composed
when the core controller is created. Lower modules do not import `core_runtime`.
Adding a server strategy requires implementing `CoreBackend` and registering it
in `ocd/registry.py`, not adding branches to the controller or DAP command.

CPU selection is separate from AP transport: the existing `DebugPortTransport`
interface still owns AP transactions and architectural AP decoding stays in
the `arch` package. pyGdbServer retains executable argument generation, port
allocation and process supervision because they precede GDB-side detection.
See [SMP support](smp.md) for runtime behavior and configuration.

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

Identification has been verified with J-Link V9.82 on the i.MX8MP Cortex-M7
and AArch64 contexts. The J-Link AP transport supports the
verified ADIv5 JTAG-DPv0 configuration, where DP SELECT can be read back.
It uses `ReadAPEx` to inspect AP registers, saves SELECT, and restores and
verifies it even when a read fails. The restoration writes only the debug-port
selection register, not AP transfer registers or target memory. SWD/other DP
versions report explicit errors. CPU context switching uses the separate core
backend contract, not AP selection.

### Attached Cortex-M Identity

After a successful connection, pyGdbServer extracts the detected Cortex-M name
from JLinkGDBServer output (`JTAG ID: 0x... (Cortex-M...)` or
`Found Cortex-M...`), requires `Connected to target`, and registers it with
`CORES.register_jlink_core(name)` inside GDB. Device settings alone are not
accepted, nor are failed or conflicting detections. These are connection-log
formats, not a standardized core-identification protocol. Unknown formats
fail closed without blocking ordinary server startup.

`dap core`, `dap core list` and the matching RPCs then expose the single
attached CPU. RPC metadata contains `scope: attached-core-only` and
`identity_source: jlink-server-connection-log`. The local ID `0` is not a
SEGGER physical CPU identifier. Selecting it has no side effects; other IDs
are rejected. No AP or target-memory operation is needed. This works with
recognized Cortex-M names independently of the board's device identifier.

J-Link attached-core registration is managed by **pyGdbServer**. Correct identification requires
correlating the OCD's connected CPU with GDB's actual inferior/connection,
not merely recognizing the probe or reading the configured device name.
pyGdbServer supervises both OCD and GDB, captures the connection evidence,
and binds that evidence to the GDB session to keep the whole stack coherent.

Standalone GDB does not automatically receive this correlated information.
The low-level registration API does not, by itself, establish that correlation
and is not a substitute for pyGdbServer's supported workflow on these targets.
The declaration is scoped to the current inferior/connection, not transferable
to a later connection. This connection-log parser remains Cortex-M-specific;
configured multicore endpoints use `JLinkCoreBackend`'s cluster mode instead.
Architecture-dependent identity evidence belongs to that backend, not to
the common controller. See [SMP support](smp.md) for full and partial affinity
evidence and its limitations.

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
