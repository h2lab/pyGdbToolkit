<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->

(smp-support)=
# SMP and Multicore Debugging

The toolkit provides one CPU-context selection interface for systems with
multiple processing elements. The mechanism depends on the debug server's
exposed contexts, not a particular SoC, CPU model, number of cores, or flat
affinity layout. It can be used when debugging SMP applications, but selecting
a core does not configure the target operating system or enable SMP hardware.

## Common Interface

```gdb
dap core list
dap core
dap core 1
info registers pc
dap core 0
```

`dap core list` lists server-discovered or explicitly configured CPU contexts.
`dap core` reports the current context. `dap core N` selects a known non-negative
core ID. IDs are not GDB thread numbers, inferior numbers, AP indices, or an
architecture's affinity values. Configured IDs may be non-contiguous.

The same mechanism is available through `command.execute` and the structured
RPCs `target.cores`, `target.core`, and `target.select_core` with `{"core": 1}`.
Each `CoreInfo` includes the ID, context name, selected flag, endpoint,
inferior/thread association, inventory scope, and identity evidence source.
Configured device names are selectors supplied by the user, not decoded CPU
model evidence; use `lscpu` for architectural CPU identification.

## Context Models

| Model | CPU representation | Selection | Current backend |
|---|---|---|---|
| Shared GDB connection | Named hardware-core threads | Switch GDB thread | OpenOCD |
| Per-core GDB endpoints | One inferior per attached CPU | Attach or reuse an inferior | pyOCD, configured J-Link sessions |

The portable controller owns request validation, backend dispatch, result
verification and session cache invalidation. The shared GDB context layer owns
inferior attachment, reuse and attachment rollback. Backend strategies supply
inventory, selection policy, endpoints and identity evidence. Architecture-specific checks must not assign core IDs
by assuming a CPU model or interpreting an affinity value as an array index.

### Implementation Boundaries

The `ocd/` package contains `jlinkgdbserver.py`, `openocd.py`, and `pyocd.py`,
each implementing the same `CoreBackend` interface from `ocd/base.py`.
`ocd/registry.py` resolves the detected server; `core_runtime.py` uses this
contract without conditional branches for individual servers. Thread-name
parsing, endpoint conventions and identity register reads stay in the owning
backend. Common GDB inferior operations live in `ocd/context.py` and are
injected into backends through the `CoreContextAccess` protocol.

The existing public detection API remains available through
`from pyGdbToolkit.ocd import get_ocd`. Detection and selection are separate:
`ocd/detection.py` caches server identity per connection without owning CPU
inventory or opening extra endpoints. pyGdbServer remains responsible for
starting and stopping processes. See [OCD backends](ocd.md) for the hierarchy
and extension contract.

### Selection Lifecycle

```text
CLI or RPC request
  -> backend inventory or configured endpoint map
  -> validate requested core ID
  -> switch hardware thread OR attach/reuse a GDB inferior
  -> collect available backend/architecture identity evidence
  -> verify stable context identity
  -> return CoreInfo and invalidate target-memory/architecture caches
```

Endpoint attachment copies the active GDB architecture and executable symbols
to a new inferior. Consequently this path assumes compatible cores and the same
program image; heterogeneous systems requiring distinct architectures or ELF
files need additional per-core configuration and are not automatically handled.
An attachment failure restores the previous context and removes the new
inferior. Existing attachments are reused only while their inferior/connection
identities remain valid. Listing does not create new GDB connections.

## Backend Configuration

`ocd-path` chooses the executable. `ocd-identifier` chooses its backend protocol:

```json
{
  "ocd-path": "/usr/local/bin/debug-server-wrapper",
  "ocd-identifier": "jlinkgdbserver"
}
```

Valid identifiers are `jlinkgdbserver`, `openocd`, and `pyocd`. The declared
identifier controls startup arguments, readiness detection, and remote protocol
selection even when the executable is renamed or wrapped. GDB-side detection
still checks the connected server; configuration is not proof of a live backend.
For legacy files without this field, known executable names are inferred.
Unknown legacy executables retain the generic loopback/port-template rules.

### OpenOCD

OpenOCD must expose physical cores as named hardware threads on one SMP GDB
connection. The adapter recognizes indexed names ending in `cmN`, `cpuN`,
`coreN`, or `rvN`, for example `target.cpu0`. It does not infer physical cores
from thread enumeration order or mistake RTOS task threads for CPUs. Ambiguous
or unrecognized names fail explicitly. Group creation and all-stop behavior
remain responsibilities of the OpenOCD target configuration.

### pyOCD

pyOCD inventory comes from `monitor show cores`. The initial connection must
use the lowest GDB port, corresponding to core 0. The current adapter uses
`initial_port + core_id` for other endpoints. Inferring these ports is a pyOCD
convention, not a general SMP rule. Inferiors are attached lazily and reused.

### J-Link

Configured multicore sessions use one supervised JLinkGDBServer per device
selector. This is separate from the attached-Cortex-M mode, which reports only
the single server-identified CPU with `scope: attached-core-only`.

`jlink-core-devices` is accepted **only** when the resolved backend identifier
is `jlinkgdbserver`, including when the node is empty. The recommended form
explicitly maps IDs to device selectors supported by the installed SEGGER pack:

```json
{
  "ocd-path": "JLinkGDBServer",
  "ocd-identifier": "jlinkgdbserver",
  "ocd-args": ["-device", "VendorCoreB", "-if", "JTAG"],
  "jlink-core-devices": {
    "7": "VendorCoreA",
    "12": "VendorCoreB"
  }
}
```

This is a configuration fragment; replace the illustrative selectors with
supported devices and complete the other fields as shown in the
[configuration examples](configuration-examples.md). The initial core is the ID
whose selector equals `-device`: 12 in this fragment. Device spelling, list
order, and CPU architecture do not determine the ID. Selectors and IDs must be
unique. The old array form remains readable for devices with numeric `_N`
suffixes, but an explicit mapping avoids that naming convention entirely.

pyGdbServer starts each configured server, allocates distinct loopback GDB,
Telnet, and SWO ports, waits for its target-connected log, and passes the
endpoint map to the GDB-side controller. GDB attaches additional inferiors on
first selection. All supervised processes are stopped during server shutdown,
including partially started sessions. A standalone session without this
registration does not automatically discover a cluster.

The `-swoport {swo_port}` placeholder also supports dynamic SWO allocation for
single-core J-Link sessions. Use it with `-port {gdb_port}` and
`-telnetport {telnet_port}` when running multiple independent server instances.

Each instance uses `-noreset -noir`; reset/load `gdb-init` commands and `-ir`
register initialization are rejected for configured clusters. Opening a server
can still halt its CPU or run device-specific connection sequences. All
configured devices must already be accessible and powered. No generic CPU
power-up, CoreSight scan, or secondary-core boot sequence is performed.
Multiple sessions on one physical probe must be supported by the target and
SEGGER configuration; the generic schema does not certify every device.

## Identity Evidence

A configured inventory has `scope: configured-core-cluster`, distinct from a
hardware-discovered inventory. Before attachment, evidence is `configuration`.
The J-Link adapter records architectural evidence when it can read it:

- On AArch64, named GDB `MPIDR_EL1` access retains all four affinity levels,
  including `Aff3`. The ID is bound to the observed affinity, not compared with
  that affinity as an integer core number.
- If named access is unavailable, the verified J-Link CP15 read of the MPIDR
  alias supplies only Aff2:Aff1:Aff0. This is labelled `mpidr-low24`. It cannot
  distinguish CPUs differing only in Aff3; collisions are rejected rather than
  inventing distinct identities. Unknown high affinity bits are not inferred.
- On architectures without that implemented identity reader, evidence remains
  `configured-endpoint`. Distinct sockets are not proof of distinct physical
  CPUs. The selectors must be correct, and architecture-specific verification
  can be added in the backend adapter without changing portable attachment.

Observed identities must be stable across repeated pivots and distinct among
attached configured CPUs. Errors do not silently redirect to another core.
This verifies consistency with the configured endpoint; it does not derive a
SoC-wide topology, cluster count, or expected physical ID from a device name.

## Halting and Breakpoints

Context selection and execution control are distinct. Once selected, normal
GDB commands operate through that CPU's context. Halt/interrupt/resume syntax
and synchronization policies depend on the backend. For J-Link, `monitor halt`
and `monitor IsHalted` act on the selected connection. Hardware breakpoints
can be inserted through GDB; use inferior restrictions when they must remain
local to one CPU. Software breakpoints may modify shared code memory.

The toolkit does not provide atomic all-core halt/resume, CTI cross-trigger
configuration, or SMP scheduler/task awareness. A shared-thread backend may
offer stronger synchronization through its own configuration; independent
endpoint sessions do not automatically acquire it. Halting individual CPUs
can stall an OS, hold locks, or trigger watchdogs. Cache coherency, address
translation, and debug authentication remain target/backend responsibilities.

## Validation Scope

Unit tests cover both context models, backend identifiers, arbitrary selectors,
non-contiguous IDs, full/partial affinity evidence, reuse, rollback, and cleanup.
They do not establish hardware support for every possible selector.

Hardware validation currently includes RP2350 contexts through OpenOCD/pyOCD
and an i.MX8MP A53 cluster through J-Link V9.82 with gdb-multiarch 16.3. The
J-Link test verified four distinct affinities, repeated CLI/RPC pivots, halted
state and hardware breakpoint insertion/removal, not breakpoint hits under
resumed SMP execution. Board-specific fixtures stay in examples and hardware
tests, not in the portable SMP model. These tests require explicit permission
because even debug reads and attachments can affect a running target.
