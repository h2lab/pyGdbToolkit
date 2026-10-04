# Memory Mapping

`memmap` collects declared memory regions and measures read accessibility at
selected points. It works through the current GDB inferior and core, including
architectures without a toolkit target-identification provider.
Discovery supplements the shared session with vendor, SoC-family and memory
evidence when an unambiguous hardware fingerprint is available.

## First Session

Connect GDB to the target and stop its execution. Load the ELF and, optionally,
the device SVD with `svd load`. Neither is required to collect a server map.

```gdb
memmap discover
memmap discover --verify
memmap show
memmap show --brief
memmap probe --known --max-reads 256
memmap report mapping.json
```

`discover` replaces the previous map and samples. It collects metadata and,
on supported architectures, reads documented CPUID/CoreSight/manufacturer
identification registers. It does not scan the candidate memory windows.
Unsupported sources produce notes rather
than preventing the other sources from being used. If no source supplies a
region, the map remains unknown; no full-address-space scan is started.

`--verify` additionally samples up to two aligned four-byte points in each
eligible declared region, near its start and exclusive end. The global budget
is 256 reads and five seconds between reads. MMIO/OTP descriptions are excluded
using the same rules as `probe --known`. The displayed region table then includes
readable/error counts or `not-probed`, without claiming whole-region access.

Discovery also reads PC and SP from the current stopped GDB frame, so it works
after startup as well as at reset. On Cortex-M it inspects supported VTOR banks
and up to four entries of each active vector table: initial SP, Reset, NMI and
HardFault. Reads are bounded and individual access failures remain visible.

`show` and `report` use the last snapshot without reading target memory. The
snapshot retains its original access context, even after changing cores or
disconnecting. Run `discover` again before probing under a different context.

`memmap show --brief` prints only one probable memory-map table: start,
exclusive end, kind, basis and access-sample counts. It includes declared
regions and runtime-supported candidates, labelled separately. Unconfirmed
or conflicting candidate windows are not added to this concise table.
Candidate bounds remain hypotheses, not physical memory sizes. Identity,
execution, candidate-analysis and point-observation tables are omitted;
the full evidence is retained for `memmap show` and JSON export.
`--brief` is supported only by `show` and never changes discovery or sampling.

## Commands

```text
memmap discover [--vendor <manufacturer>] [--verify]
memmap show [--brief]
memmap bases [<manufacturer>]
memmap probe --known [--stride <bytes>] [--max-reads <n>] [--timeout <seconds>]
memmap probe --range <start>:<end> --allow-unsafe [--stride <bytes>] [--max-reads <n>] [--timeout <seconds>] [--ignore-memory-map]
memmap report <output.json>
memmap help
```

Addresses and stride accept decimal or `0x` hexadecimal integers. Ranges are
half-open: the end address is excluded. Starts must be aligned to four bytes;
stride must be a positive multiple of four. Each sampled point reads at most
four bytes and never extends past the range end.

Default limits are a 4096-byte stride, 256 reads and five seconds. The read
budget is global across all selected ranges, not per region. The maximum read
budget is 4096 and the maximum time budget is 300 seconds. The time budget is
checked **between reads**; it cannot interrupt a blocked transport operation.
Use GDB interrupt or the transport's timeout when necessary.

The report records the ranges, stride, budgets, timestamp, actual number of
reads and stop reason for each completed probe run. Overlapping sample
addresses are read once per run. Samples from successive runs are retained.
Regions are visited in discovery order, so a small budget can leave later
regions entirely untested.

## Discovery Sources

| Source | Evidence | Automatic probing |
| --- | --- | --- |
| GDB remote `qXfer:memory-map:read` | Server-declared RAM, ROM and Flash ranges | Eligible, subject to conflicting MMIO/OTP exclusions |
| ELF section inventory | Allocated section addresses and extents | No: program placement is not a physical memory map |
| Loaded SVD | Envelopes of described peripheral registers | No: reads can have side effects |
| DAP profiles | AP identity, capabilities, ROM-table address and access errors | No: these do not describe all physical memory regions |
| Runtime PC/SP and ARM vector registers | Current code, stack, vector-table and handler addresses | Address evidence only; not a physical memory size |

SVD envelopes include the gaps between described registers. They do not claim
that the gaps exist, are readable, or describe a peripheral's complete size.
SVDs with non-eight-bit address units are not used for mapping.

Conflicting declarations are retained as separate evidence. A server-declared
memory region overlapping a known register or OTP region is excluded entirely
from `--known`; it is not silently trimmed or preferred over the other source.
The current sources do not automatically identify OTP. Unknown server memory
types remain unknown and are not automatically probed.

Explicit Cortex-M architecture names receive architectural address-space
hints, such as SRAM space or peripheral space. These are **not** assertions of
physical RAM, Flash, OTP, or accessible memory. Other architectures use a
generic provider without ARM assumptions.

## Manufacturer Reference Windows

The toolkit includes broad candidate windows for major manufacturer families,
not the exact capacities of individual ordering codes:

```gdb
memmap bases
memmap bases st
memmap bases nxp
memmap bases infineon
memmap bases xilinx
```

The command works without a connected target or a prior `discover`. It lists
the manufacturer, broad family, CPU architecture, possible memory kind, start,
exclusive end, reference document and caveats. Manufacturer filters are
case-insensitive; `STMicroelectronics`, `NXP Semiconductors`,
`Infineon Technologies`, `AMD` and `AMD/Xilinx` aliases are accepted.

| Manufacturer | Broad families | Examples of candidate windows (exclusive ends) |
| --- | --- | --- |
| ST | STM32 | Flash/aliases `0x08000000:0x10000000`; boot ROM `0x18000000:0x18100000`; SRAM/TCM `0x20000000:0x20400000`; AXI SRAM `0x24000000:0x25000000`; possible secure aliases `0x34000000:0x35000000` |
| NXP | LPC / Kinetis / i.MX RT | Flash/boot aliases `0x00000000:0x02000000`; local SRAM `0x10000000:0x10100000`; SRAM/TCM/OCRAM `0x20000000:0x20400000`; external apertures `0x60000000:0x80000000` |
| NXP | i.MX application processors | Possible OCRAM `0x00900000:0x01000000`; DDR aperture `0x80000000:0x100000000` |
| Infineon | XMC | Flash windows `0x08000000:0x0A000000` and `0x0C000000:0x0E000000`; PSRAM `0x10000000:0x10100000`; DSRAM `0x20000000:0x20100000` and `0x30000000:0x30100000` |
| Infineon | PSoC / TRAVEO | Possible Flash `0x10000000:0x11000000`; SRAM candidates `0x08000000:0x09000000` and `0x28000000:0x29000000` |
| AMD/Xilinx | Zynq / Zynq UltraScale+ | Low DDR/boot aperture `0x00000000:0x80000000`; on-chip RAM aliases `0xFFFC0000:0x100000000`; QSPI aperture `0xFC000000:0xFE000000`; high DDR `0x800000000:0x1000000000` |

These are **hypotheses**, not universal maps for an entire manufacturer.
In particular, other STM32, NXP, Infineon and AMD/Xilinx families can have very
different layouts. Apertures are address windows, not installed memory sizes.
Flash and OCM aliases do not represent additional physical capacity. Reserved
addresses, holes and runtime-reserved bytes may be included. The same address
can suggest different kinds on different families, especially across XMC and
PSoC/TRAVEO; the catalog does not resolve these alternatives by itself.

Discovery automatically selects a manufacturer's candidates when an identity
is available. Without an identity, a filter can be supplied explicitly:

```gdb
memmap discover --vendor nxp
```

A filter is not a fingerprint. A filter conflicting with a hardware-confirmed
manufacturer is rejected. Each candidate is challenged against source metadata
and exact samples: `declared-overlap`, `kind-conflict`, or `unconfirmed`, with
readable/error point counts. These labels do not establish physical sizes,
continuity, absence of memory, or protection.

Candidates are exported separately from `regions` and never become eligible for
`probe --known` merely because they are in a manufacturer's catalog. Read the
exact device manual before explicit sampling, which still requires
`--allow-unsafe`. Catalog references describe family memory-map patterns, not
certified capacities for a selected device.

## Fingerprint And Shared Session

The discovery output includes manufacturer, SoC family, architecture, identity
provenance and confidence. There are three identity states:

- `hardware-confirmed`: a validated MCU ROM identity and an unambiguous
  manufacturer-provider match establish the family.
- `server-reported`: complete, consistent server metadata is available, but
  independent hardware confirmation is unavailable.
- `conflict`: hardware and server metadata disagree; no confirmed session
  identity is published.

  ## Runtime Addresses And Missing ROM Regions

  The execution-evidence table and JSON `execution_hints` preserve the origin,
  address, role, evidence, matching regions and matching candidate windows.
  Unmapped addresses remain visible. A candidate base is shown as a hypothesis;
  it is not promoted to a discovered physical extent just because PC lies inside.

  PC identifies the current code location; SP identifies the current stack top.
  SP may equal a region's exclusive end, so stack association uses the byte below
  SP. Neither pointer proves Flash or RAM technology. Firmware can execute from
  SRAM or external memory and move its stack after startup.

  For ARM Cortex-M, VTOR provides the current **vector-table base**, not necessarily
  the base of the underlying memory bank. Relocated vector tables in RAM are
  supported. On security-capable cores the visible VTOR and VTOR_NS banks are
  reported separately, subject to access permissions. Initial SP, Reset, NMI and
  HardFault entries are pointer evidence only. Invalid Thumb pointers, zero/all-one
  entries and read failures are retained as notes, not valid handlers. Vector
  tables pointing into peripheral or known OTP/register windows are not followed.
  The command does not invoke handlers or modify VTOR, PC, SP or target memory.

  ARMv8-M cores retain a general ROM/Flash candidate starting at `0x18000000`,
  even without a recognized manufacturer. The hypothesis window
  `0x18000000:0x19000000` is not a physical capacity. It can represent ROM,
  Flash or a code/security alias on some SoCs; this placement is not required
  by the ARMv8-M architecture and alias relationships are not assumed.
  The STM32 manufacturer catalog also retains this `rom-or-flash` possibility
  across product lines rather than limiting it to STM32N6.

  PC, VTOR or valid handler pointers in this window produce `runtime-supported`
  evidence and are listed in `execution_origins`. Without supporting evidence
  the window remains `unconfirmed`. Server ROM and Flash declarations are both
  compatible with this hypothesis; a RAM declaration is reported as a type
  conflict, without overriding the declared type.

  No STM32N6-specific 128-KiB region or secure/non-secure alias is injected into
  the discovered map. Candidates remain separate from `SESSION.memory_regions`
  and are not automatically sampled by `--verify` or `probe --known`. In
  particular, executing code at `0x18000000` does not prove programmable Flash,
  physical boundaries, or protection properties.

The current hardware-confirmation provider reuses Cortex-M CPUID/CoreSight and
the existing STM32 manufacturer registry. Other architectures/manufacturers
remain extensible through the provider contract. Their configured names or
candidate addresses are not silently promoted to hardware-confirmed identities.

For example, a live STM32N657 session can confirm JEP106 ST, MCU-ROM part `0x486`
and Cortex-M55, yielding **STM32N6 product line**. `STM32N657I0HxQ` is retained
separately as the **server-reported** ordering code; the MCU ROM does not prove
that exact ordering code. Unique serial numbers are not exported.

When the fingerprint is hardware-confirmed, discovery publishes its report in
`SESSION.discovery`, its fingerprint in `SESSION.target_info`, and its declared
regions in `SESSION.memory_regions`. Samples and candidate assessments remain
available through the shared report. This supplements, rather than replaces,
the architectural `SESSION.probe()` result. Failed/uncertain fresh discovery
clears the previous shared identity instead of reusing it.

Target/core/connection/object-file context changes, a loaded SVD replacement,
`SESSION.invalidate()` or `SESSION.reset()` invalidate the shared discovery.
The command's original snapshot remains available for `show` and `report` until
replaced or reset. See [session](session.md) for the Python API.

## Explicit Exploration

When no eligible declared region is available, a range can be supplied:

```gdb
memmap probe --range 0x20000000:0x20010000 --allow-unsafe --stride 0x1000 --max-reads 16
```

Only run this after deciding that reads of the range are acceptable for the
specific target. `--allow-unsafe` acknowledges that an arbitrary range may
contain MMIO, OTP controllers, FIFOs or registers with read-clear behavior.
It does not make the reads safe. Server declarations are also not a guarantee
of side-effect-free access; review the target configuration before `--known`.

GDB can reject an address absent from its memory map before contacting the bus.
For a reviewed explicit range, `--ignore-memory-map` temporarily disables GDB's
local `mem inaccessible-by-default` policy and restores its original setting,
including when a read fails. It does not bypass target protection or change APs.

```gdb
memmap probe --range 0x18000000:0x18000004 --allow-unsafe --ignore-memory-map
```

Fingerprinting and bounded runtime-vector discovery use the same temporary
local-policy bypass. The original setting is restored on failures as well as
success. `--verify` and ordinary `probe --known` do not disable this policy or
sample candidate windows absent from the declared map.

No target-memory writes, reset, unlock, mass erase or automatic scan of the
entire address space is performed. Sampling requires all threads of the
selected inferior to be stopped; other inferiors and physical cores are not
automatically halted.

## Interpreting Results

Declarations and observations are displayed separately:

- `kind` records a declared type, or `unknown`; its source is always retained.
- `readable-sampled` means the selected GDB view returned bytes at this point.
- `read-error` retains the exact access error, without assigning a cause.
- `protection` remains `unknown` in this version: no device-specific protection
  decoder is implemented.

An untested region has no samples. A successful read does not establish write
access, executable access, absence of protection, or continuity between sample
points. GDB or its server may cache reads or reject them before a bus transaction.
An error can indicate missing memory, a power/clock issue, a security restriction,
a server policy or a transport problem. It is not proof of protection.

Sampling does not determine exact physical boundaries, detect all small regions,
or distinguish memory aliases. MPU/SAU permissions and security attribution must
not be treated as the debug port's effective access permissions. Use
[secscan](secscan.md) for the separate security-posture audit.

## Core And AP Context

`memmap` records the architecture, inferior, connection, endpoint, process,
selected thread and loaded object files. A context change or replacement of
the loaded SVD requires a fresh discovery before further sampling. Sampling
constructs a fresh memory reader for the current inferior.

`dap select` changes the monitor AP selection; it does **not** reroute ordinary
GDB memory reads. `memmap` therefore does not accept an `--ap` option. Choose
the CPU view with `dap core`, then run `memmap discover` again. The DAP profile
in the JSON report is discovery-time metadata, not proof of the route taken by
each later read. See [dap](dap.md).

## JSON And Client Access

`memmap report` exports schema version 1, the original access context, source
metadata, fingerprint confidence and evidence, separate candidate assessments,
runtime address evidence and region/candidate associations,
DAP evidence when available, exact sample addresses and errors, and probe-run
limits. Explicit probe runs record whether the local GDB policy was bypassed.
Sampled memory contents are not exported. The output directory
must already exist; quote paths containing spaces.

The same commands can be executed through pyGdbServer's `command.execute` RPC.
No additional structured memory-map RPC is introduced in this version.

`memmap` is registered alongside the other toolkit commands in
`toolkit.commands`; its full syntax and safety notes are returned by
`toolkit.help` with `{"command":"memmap"}`. In pyGdbClient, enter the command
unchanged in the normal prompt. Its Rich tables, identity evidence, errors and
sampling results appear in the common command-output panel. The client does
not intercept memory commands or parse their arguments.

```json
{"jsonrpc":"2.0","id":1,"method":"command.execute","params":{"command":"memmap discover --verify","timeout":60}}
{"jsonrpc":"2.0","id":2,"method":"command.execute","params":{"command":"memmap bases nxp"}}
{"jsonrpc":"2.0","id":3,"method":"command.execute","params":{"command":"memmap report \"reports/mapping.json\""}}
```

Report paths are resolved by GDB on the **server**, not in the client's
filesystem. The destination directory must already exist. The RPC `timeout`
bounds command execution; a probe's CLI `--timeout` is its separate sampling
budget, checked between reads. A transport timeout does not imply cancellation
of an in-progress GDB operation. All clients share the same GDB session and
latest memory-map snapshot. The discovery and probing guardrails remain in
the toolkit command, just as in a direct GDB session.

## Extension Points

The portable region/provider contracts live in `arch/memmap.py`; the registry
includes a generic fallback. Cortex-M window interpretation lives exclusively
in `arch/arm/memmap.py` and is registered by the runtime, not by the command.
ARM manufacturer reference data lives in `arch/arm/memmap_baselines.py`; its
portable `MemoryBaseline` contract and catalog filtering live in `arch/memmap.py`.
The sampling engine is independent of the GDB CLI. GDB server, ELF and SVD
metadata adapters live in `memmap_runtime.py`.

Future device providers can supply independently evidenced Flash/RAM/OTP
descriptions and protection observations. Such support must distinguish CPU
permissions, security attribution and debug-access restrictions. AP-routed
sampling requires a dedicated MEM-AP memory transport before it can be exposed
as a CLI option.
