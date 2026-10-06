# Access Ports

`dap` inspects the Access Ports of the target connected to the current GDB
inferior. The toolkit automatically detects pyOCD, OpenOCD or J-Link and uses its public
`monitor` commands over the existing GDB connection. No second probe connection
is opened. ARM ADIv5/APv1
and ADIv6/APv2 are supported by pyOCD/OpenOCD, independently of JTAG or SWD wiring.
J-Link AP inspection currently supports the verified ADIv5 JTAG-DPv0 backend.

## Commands

```gdb
dap list
dap core [list|<id>]
dap select <index-or-address>
dap profile [<index-or-address>]
dap report <output.json>
dap help
```

## CPU Core Selection

Core selection changes the actual GDB CPU context, not only the Access Port
used by monitor commands:

```gdb
dap core list
dap core
dap core 1
info registers pc
fault_info
dap core 0
```

`dap core list` discovers physical cores, shows their endpoints, attached GDB
inferiors/threads and marks the active core. Discovery does not attach additional
sockets. `dap core` reports the active core; `dap core <id>` selects a discovered
non-negative decimal core ID. Ordinary GDB commands and toolkit diagnostics then
use this core. Cached target memory and architecture identity are invalidated.

With **pyOCD**, initially connect GDB to the lowest TCP port, corresponding to
core 0. Core N uses the same host and port `initial_port + N`. The toolkit reads
`monitor show cores` for inventory, attaches each additional core lazily in a
separate GDB inferior and reuses that connection on later selections. The new
inferior receives the original architecture and executable symbols, if loaded.
Both `target remote` and `target extended-remote` are supported. An attachment
failure restores the previous GDB context and removes the newly created inferior.

With **OpenOCD**, the target configuration must expose physical cores as named
hardware threads on one SMP connection. The RP2350 configuration exposes
`rp2350.cm0` and `rp2350.cm1`. Selection switches the corresponding GDB thread;
it does not open another socket. Unidentifiable threads, including RTOS task
threads that cannot be mapped to physical CPUs, cause an explicit error rather
than guessing from thread order or GDB thread IDs.

With **J-Link**, pyGdbServer registers the server-reported Cortex-M attached
to the current connection. `dap core list` shows one selected entry with
`scope: attached-core-only`; `dap core` reports it, and `dap core 0` is a no-op.
The ID `0` is local to this session, not an APSEL or a SEGGER physical-core ID.
Other IDs are rejected. No RTOS thread switching, AP scanning, memory reads,
reset or additional probe connection is performed by these core operations.

On complex targets such as the **i.MX8M family**, reliable J-Link `dap core`
support requires **pyGdbServer**. It correlates the CPU reported by the OCD
with the actual GDB inferior/connection and manages both processes as one
coherent debug session. A standalone GDB connection or a configured device
name alone cannot provide this correlation; standalone J-Link `dap core`
operation is not supported for these targets.

The identity comes from successful JLinkGDBServer connection logs, not a
board-name table or the configured `-device` alone. Missing, conflicting or
unrecognized log formats leave it unavailable. The declaration is bound to
the GDB inferior/connection pair and becomes invalid after reconnection.
Cortex-A/AArch64 and Cortex-R are outside this implementation's scope.
This does not discover all physical CPUs in a multicore SoC.

SEGGER's [UM08036 protocol extensions manual](https://www.segger.com/downloads/jlink/UM08036)
(V1.00) documents trace/SWO queries, not a core-inventory query or a physical
CPU-number query. Consequently no undocumented remote packet or `monitor core`
fallback is used. See [ocd.md](ocd.md) for identity registration details.

Selection does not explicitly reset or resume the target, nor change APSEL.
Attaching a new pyOCD socket can halt its core according to the server's connection
policy. The active AP is rediscovered on the next AP operation because a server
can change its monitor MEM-AP when the CPU context changes. `dap select` retains
its AP-only semantics.

The same commands work through pyGdbServer's `command.execute`. Structured RPCs
are also available: `target.cores`, `target.core`, and `target.select_core` with
`{"core": 1}`. See [pygdbserver.md](pygdbserver.md).

### Hardware Validation

The opt-in RP2350 tests use the two example board configurations, requiring the
configured probe and compatible pyOCD/OpenOCD executables. They test standalone
GDB and real WebSocket RPCs for both backends, repeated core switching, register
reads, AP discovery and pyOCD connection reuse:

```console
PYGDB_CORE_HARDWARE=1 .venv/bin/python -m pytest -q tests/test_core_hardware.py
```

The normal test suite skips these six hardware tests: standalone GDB uses both
`remote` and `extended-remote` for each backend, and RPC tests cover both backends.

## AP Operations

`list` uses pyOCD's discovered inventory, or OpenOCD's DAP discovery. OpenOCD
reads the bounded ADIv5 APSEL range 0..255 and traverses the ADIv6 root ROM table.
J-Link reads the bounded APSEL range 0..255 using `ReadAPEx <APSEL << 24> 0xFC`.
Only DPv0/JTAG is accepted, where SELECT can be read back safely. SELECT is
saved before every scan or register read, restored using `WriteDP 2`, and
verified afterwards, including error paths. No AP CSW/TAR or target-memory
writes are issued. Responding indices may include aliases; they are not a
guarantee of distinct physical APs.
Neither method guarantees discovery of hidden, powered-off or locked APs.
APv1 identifiers are decimal APSEL indices, while APv2 identifiers are displayed
as hexadecimal base addresses. Both decimal and `0x` arguments are accepted.

`select` changes the MEM-AP used by pyOCD's monitor memory operations, or the
current OpenOCD DAP's `apsel`, and
verifies the server selection. It does **not** change the GDB CPU core or reroute
GDB's regular memory packets, which remain bound to the server's core. Selecting
another core in pyOCD can override this selection. Non-memory APs cannot be
selected by the pyOCD backend, but can be listed and profiled.

For J-Link, `select` changes only the toolkit's profiling selection. Initially
this is taken from DP SELECT; an explicit selection is kept locally and does
not change the GDB memory view. No server-side MEM-AP selection is claimed.

`profile` reads the specified AP, or the server's currently selected AP if no
argument is supplied. It decodes IDR, including JEP106 designer, class, type,
variant and revision. MEM-APs additionally expose CFG, CSW and BASE, and BASE2
when large addressing is advertised. This identifies AHB, APB, AXI and their
ARMv5 bus variants, address/data extensions, current transfer configuration,
device enable state and the ROM table base. APv2 profiles also decode the error
mode, DAR window size and auto-increment page size.

The architecture profiler does not write AP registers, access target memory,
scan the ROM table, or test transfer sizes by changing CSW. OpenOCD discovery,
which runs before profiling, does traverse ROM tables and can modify MEM-AP
transfer registers while accessing component memory. A current 32-bit setting does
not mean only 32-bit transfers are supported. Memory access capability does
not guarantee read/write access to all regions: bus, power and security
permissions still apply. Inaccessible registers retain their errors and unknown
capabilities use JSON `null`, rather than a fabricated negative result.

`report` collects fresh profiles for every discovered AP without changing the
selection and writes UTF-8 JSON. Paths with spaces must be quoted. The output
contains `schema_version`, UTC `generated_at`, GDB `architecture`, backend,
discovery source, `selected_ap`, and `access_ports`. Each port contains its
numeric identifier (`index`; an APv2 base address when `ap_version` is 2),
server name, selection flag, decoded type and identity, capabilities, raw
register values, and per-register errors. Numeric values are JSON integers.

## Pico 2 W

The supplied configuration can start the usual supervised session:

```console
pyGdbServer doc/examples/boards/pico2w.json
```

For the same tests with OpenOCD:

```console
pyGdbServer doc/examples/boards/pico2w-openocd.json
```

The OpenOCD example listens at `localhost:1235` and selects the CMSIS-DAP probe
`E6647C74034BC430`. Note that an openocd build with
RP2350/ADIv6 support is required. Only one server can use this probe at a time.

Once connected, run these commands in GDB or the pyGdbClient command prompt:

```gdb
dap list
dap profile 0x2000
dap select 0x4000
dap profile
dap report pico2w-aps.json
dap select 0x2000
```

On the tested RP2350 in ARM mode, both backends discover two ADIv6 AHB5-APs at
`0x2000` and `0x4000`, both with IDR `0x34770008` and ROM base `0xE00FF000`.
The underlying monitor reads use absolute APv2 addresses, for example
`monitor readap 0x2dfc`. APv1 uses `monitor readap <APSEL> <offset>` instead.
OpenOCD uses `monitor echo [rp2350.dap apreg 0x2000 0xdfc]` for the same IDR.
The command does not reset the target or program flash; connecting a debug
server/GDB can halt execution according to their configuration.

## Extension Points

- `debug_port.py`: architecture-neutral port descriptors, transport protocol,
    transport errors, and pyOCD/OpenOCD monitor adapters. Other servers can implement
  `DebugPortTransport` without adding register decoding to the command.
- `arch/dap.py`: portable profile model and architecture provider registry.
- `arch/arm/dap.py`: ARM AP register layout, identity and capability decoding.
- `ocd.py`: startup OCD detection and a connection-aware identity interface;
    see [ocd.md](ocd.md).
- `ap_runtime.py`: architecture provider composition and automatic OCD transport.
- `cmd_dap.py`: argument handling, rendering, JSON export and session state.
- `core_runtime.py`: physical core discovery and GDB context selection shared
  by `dap core` and the server RPCs.

Other GDB servers, multi-DP addressing, JTAG chain configuration, downstream
JTAG-AP operations and COM-AP transactions are not implemented. Unsupported
servers and address formats produce explicit errors.
