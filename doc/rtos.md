<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# `rtos` command

The `rtos` GDB prefix groups project symbol loading, task inspection, and
scheduler tracing. **Camelot** supports all three; **Zephyr** currently supports
build loading/display, live thread inspection and supported Cortex-M saved
contexts/stacks, but not scheduler tracing. The `rtos select <name>` interface and
RTOS-specific modules keep these capabilities separate without changing command
names. Output uses Rich tables.

## Getting started

Load the toolkit in a GDB session and build the Camelot project before running
`load-project`. Use the project directory containing `project.toml`, not its
`output/build` directory:

```gdb
(gdb) py import pyGdbToolkit
(gdb) rtos list
(gdb) rtos select camelot
(gdb) rtos load-project --from /path/to/camlot/project
(gdb) rtos show
```

The last command works without a connected target: it describes the built
project, not live memory. For live task inspection, connect to the target and
stop it first:

```gdb
(gdb) rtos show task calculator-app
```

For Zephyr, select `zephyr` and load the build directory containing
`build_info.yml`, for example `rtos load-project --from /path/to/workspace/build`.
See [Zephyr support](rtos-zephyr.md) for required inputs and current limitations.

The RTOS selection, loaded project, and decoded task-list slots belong to the
current GDB session. Selecting another RTOS clears the project and its task
list; loading another project replaces them. These commands do not flash the
target or change its memory.

## Projects on a remote client

The CLI path above is always resolved on the **GDB host**, even when entered
through pyGdbClient. For a project available only on a remote client, pyGdbServer
also supports the JSON-RPC sequence `rtos.project.begin`, `rtos.project.put`,
`rtos.project.commit`, and the read-only `rtos.status` method. The server stages
only the required build files and uses the same GDB loader as the local CLI.
No shared filesystem or server-to-client filesystem access is needed.

See [the RTOS RPC protocol](pygdbserver.md#remote-rtos-projects) for parameters,
chunk limits and the versioned status format. In the pyGdbClient dashboard, the
`project <path> <type>` command drives this sequence: it validates the project
type against the shared registry (`camelot` or `zephyr`), uploads the declaration
and every manifest-required build artifact from the local `<path>`, then commits
the transfer so GDB selects the RTOS and loads the project. Standalone GDB use
does not require the server or the transfer protocol.

Before contacting the server, the client parses the declaration into the
project's software identity (`ProjectDescription`): the project name and
version and, for each component, its kind, origin (`git` or `tarball`,
repository URL without credentials, query or fragment), declared revision,
configuration, source tree and linked ELF files. For Camelot the components
are the kernel, the runtime and each `application.<name>`, with sources under
`output/src/<name>`. An invalid declaration fails locally. Every uploaded file
is recorded with its size and the SHA-256 digest of the bytes the server
received. Both feed the [audit context](llm.md#audit-context), which the
`audit [FILE]` dashboard command displays and optionally saves as JSON.

For Zephyr, use `project /path/to/workspace/build zephyr`. Its components identify
the application and kernel; the audit context records the declared Zephyr version
and artifact hashes, but does not yet authorize application/kernel/module source
access. Absolute compilation paths are not trusted source roots.

### Shared contracts and RTOS extensions

`pyGdbToolkit.shared` contains contracts usable by the toolkit, server and
client without depending on GDB. Outside GDB, importing `pyGdbToolkit` only
initializes the package namespace; GDB commands and target services are not
loaded. Inside GDB, `import pyGdbToolkit` retains automatic command registration
and the existing public exports. Import failures during GDB initialization
still propagate.

The shared RTOS package separates:

- `pyGdbToolkit.shared.rtos.models`: snapshots, manifests and path validation;
- `pyGdbToolkit.shared.rtos.registry`: the `ProjectProvider` protocol,
  `ProjectRegistry` and `DEFAULT_PROJECT_REGISTRY`;
- `pyGdbToolkit.shared.rtos.camelot`: Camelot's declaration parser and build paths;
- `pyGdbToolkit.shared.rtos.zephyr`: Zephyr's metadata parser and build paths.

To support another RTOS, implement a shared provider exposing its `name`,
`manifest(project: bytes | None)` and `describe(project: bytes)`, then register
it with the default registry. The bootstrap manifest declares `project_file`, `required` and
`required_patterns`; parsing the completed declaration expands it with the
exact build artifacts. The server uses that declaration path and provider,
not Camelot-specific branches or a fixed TOML filename. A separate registry
can also be supplied to `RtosProjects`, for example in tests.

The corresponding GDB implementation remains in `pyGdbToolkit.rtos` and is
registered through the typed `SUPPORTED_RTOS` registry. The shared provider
defines file discovery only; it does not replace GDB-side symbol loading,
metadata decoding or task inspection. GDB-side contracts live in
`pyGdbToolkit.rtos.base`:

- `RtosImplementation` validates the ELF list, primary image first, before GDB
  loads any symbols, then decodes a build context after symbol loading.
- `RtosProject` owns that context, its build-specific `RtosCapabilities`, build
  display, live thread enumeration, task inspection and scheduler observation.
- `SchedulerTrace` exposes collected observations and releases all of its
  breakpoints when cancelled. The implementation chooses its observation
  point and sampling strategy; generic commands do not assume a scheduler
  symbol, return value or architecture register.

A loaded context does not require a static task table. Its cached
`task_list_snapshot()` may return `None` when static slots are not applicable;
live task enumeration must not be performed by `rtos.status`. Unsupported live
operations are rejected explicitly according to the loaded build's capabilities.
These GDB-side contracts are not imported by the host-safe shared providers.

Camelot retains its kernel ELF requirement, static `.task_list` decoding and
return-based election sampling in its own implementation. The version 1 RPC
snapshot and Camelot command output are unchanged. Zephyr registers its own
build loader and cached context, with thread identity/state inspection enabled
for supported versions and scheduler tracing still disabled.

## Command reference

| Command | Result | Needs stopped target? |
| --- | --- | --- |
| `rtos` | Show the command reference | No |
| `rtos list` | List supported RTOS names and mark the selection | No |
| `rtos select <name>` | Select `camelot` or `zephyr` | No |
| `rtos load-project --from <path>` | Load built ELF symbols and RTOS build metadata | No |
| `rtos show` | Show build identity and linked memory mapping | No |
| `rtos show threads` | Enumerate observable Zephyr threads with explicit coverage | Yes |
| `rtos show task <identifier>` | Inspect a Camelot task or a Zephyr thread by exact name/hex address | Yes |
| `rtos showsched <num>` | Observe the next positive number of elections (Camelot) | Connected target required |

### Project symbols and mapping

For Camelot, `rtos load-project` reads `project.toml`, loads the linked kernel ELF and one
linked ELF for each declared application with `add-symbol-file`, and records
the `.task_list` section. Dummy application ELFs are not loaded. The symbols
remain usable in GDB for `info symbol`, breakpoints, and backtraces. The build
must contain the generated metadata header and layout; see
[`rtos-camelot.md`](rtos-camelot.md) for the exact paths and matching rules.

`rtos show` prints the kernel and task text/RAM ranges from the build layout,
with end-exclusive addresses and `R`, `W`, `X` permissions. For tasks with an
entry in `.task_list`, it displays every decoded field, including the slot
address, handles, and HMAC values. `idle` can be in the mapping without having
an entry in `.task_list`. This is **build-time information**, not a live
snapshot of the target.

For Zephyr, the command instead reads generated build metadata and configuration,
loads only `zephyr/zephyr.elf`, and displays its linked segments. No static task
table is assumed; see [Zephyr build inputs](rtos-zephyr.md#build-inputs).

### Live task inspection

For Zephyr, `rtos show threads` enumerates the monitored list when available;
otherwise it explicitly reports incomplete current/idle coverage. Use
`rtos show task <name-or-hex-address>` for identity, signed priority, state bits,
CPU roles and available entry metadata, plus supported stack/register context.
Names must be unique; use an address
when names collide or are unavailable. These commands require all GDB threads of
the selected target to be stopped, and never modify registers or memory.
See [Zephyr live threads](rtos-zephyr.md#live-threads) for version/configuration
requirements and failure handling.

For supported Cortex-M configurations, Zephyr uses live newest-frame registers
and GDB backtraces for the current thread in Thread mode; inactive threads show
saved core registers and symbolized PC/LR evidence without claiming a complete
unwind. Stack bounds exclude reserved top storage, and optional INIT_STACKS fill
scans are bounded and labeled as estimates. See
[Zephyr stack context](rtos-zephyr.md#stack-and-core-register-context) for supported
layouts, current-thread/ISR limits and watermark requirements.

The detailed task-table and event behavior below applies to Camelot only.

`rtos show task <taskname>` reads `task_table` in the halted kernel using
its DWARF type. It displays the task mapping, canonical job state, handle,
saved SP, stack bounds and usage in bytes and percent. Pending IPCs and
signals are labeled with their source task; queued IRQs show their number and,
if an SVD was loaded via `svd`, their canonical name. DMA events are displayed
when the built `task_t` type has a DMA queue. If an event queue is empty, the
table reports `None`.

The stack figure is derived from the current or saved stack pointer and the
kernel's generated configuration. It is an instantaneous estimate, **not a
high-water mark**. When GDB is stopped in the task's code, the command uses
the live PSP and GDB's backtrace. For an inactive task it shows the saved PC
and LR addresses and resolves symbols within that task's text range, but it
cannot promise a complete unwind of the inactive call chain. Live reads fail
if the target is disconnected or the task table has not been initialized.

### Scheduler trace

```gdb
(gdb) rtos showsched 8
(gdb) continue
```

`showsched` installs a breakpoint at `sched_elect()` and records the elected
task *on return*, after the scheduler makes its decision. After `<num>`
returns, GDB stops and prints a Rich chart: columns are numbered elections,
rows are tasks, and a filled marker marks the winner. Long traces are shown
in groups of eight elections. A new trace replaces the previous one; selecting
an RTOS or loading a project cancels an active trace. This observes actual
target execution and requires a connected target that reaches `sched_elect()`.
Cancellation also removes pending scheduler-return breakpoints. If GDB cannot
observe a scheduler return, tracing reports the failure and releases the trace
instead of continuing with an incomplete observation.

## Troubleshooting

- `Select an RTOS first`: run `rtos select camelot` before loading a project.
- `Load a project first`: run `rtos load-project --from <path>` before `show`.
- Missing `project.toml`, linked ELF, or `.task_list`: build the project and
  check that `<path>` points to its root.
- `Cannot access memory`: connect and halt a target with the matching image
  before requesting `rtos show task`.
- A breakpoint that never completes: continue the target and confirm that
  the flashed kernel matches the loaded ELF and calls `sched_elect()`.
