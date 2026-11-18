<!--
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# RTOS acceptance and capability comparison

The two backends share `rtos`, client project transfer, audit context and
controlled LLM source tools. Equivalent entry points do **not** imply identical
kernel objects or trace semantics. In particular, Zephyr support is limited to
single-image Cortex-M builds; live decoding supports Zephyr 4.1 through 4.x.

## Capability matrix

| Surface | Camelot | Zephyr |
| --- | --- | --- |
| Project input | Project root with `project.toml` and linked kernel/application ELFs | Build directory with final ELF, `build_info.yml`, `.config`, devicetree |
| `rtos show` | Static task slots and per-task text/RAM mappings | Shared image segments, board/version and observability configuration |
| Live identity/state | `rtos show task NAME` through `task_table` | Same command by unique name or address; `rtos show threads` enumerates monitored threads |
| Missing thread monitoring | Static task metadata remains available | Explicit partial current/idle enumeration, not a complete list |
| Stacks | Instantaneous occupancy, live PSP or saved SP | Instantaneous occupancy plus optional bounded `0xAA` fill estimate |
| Context/backtrace | Live GDB backtrace; inactive saved PC/LR evidence | Live core registers/backtrace; inactive core registers and saved PC/LR evidence |
| Pending events | Per-task IPC, signal, IRQ and optional DMA queues | Pending queue pointer; explicit sem/mutex/event fields and simple-list waiters |
| `rtos showsched N` | Elections observed on `sched_elect` return | Current/ready candidate pairs at exact PendSV entry, before switching |
| Remote `project` | Manifest-driven multi-ELF transfer | Exactly four build artifacts; no source or module inventory upload |
| Audit | Declared component identities and transferred artifact SHA-256 | Application/kernel identities, artifact SHA-256 and optional source discovery provenance |
| Source tools | Read-only project-relative paths | Build-relative by default; optional authorized workspace aliases for application/kernel/modules |
| Provider consent | Existing source disclosure rules | Same rules; workspace permission does not replace Copilot opt-in |

Neither backend reconstructs a full inactive call chain or proves peak stack
use, deadlocks, execution history, CPU utilization or source/binary equality.
Zephyr does not decode scalable wait trees, remaining timeout durations, all
object classes, SMP CPU bindings, userspace dual stacks or TrustZone contexts.
Camelot's IPC/IRQ model is not relabeled as Zephyr events. Unsupported operations
and missing measurements must remain explicit rather than returning invented
values. See the [Camelot](rtos-camelot.md) and [Zephyr](rtos-zephyr.md) contracts.

## Build the observable Zephyr firmware

The example is intentionally separate from an existing application or blinky
build. Download its {download}`CMakeLists.txt <examples/zephyr-rtos/CMakeLists.txt>`,
{download}`prj.conf <examples/zephyr-rtos/prj.conf>` and
{download}`src/main.c <examples/zephyr-rtos/src/main.c>`, preserving their layout,
or copy the directory from this checkout into a **new** directory inside your
Zephyr workspace:

```console
test ! -e /path/to/zephyr-workspace/pygdb-rtos-validation && \
    cp -R doc/examples/zephyr-rtos /path/to/zephyr-workspace/pygdb-rtos-validation
cd /path/to/zephyr-workspace
west build -d build-pygdb-rtos -b nucleo_u5a5zj_q pygdb-rtos-validation
```

Run West from its workspace; use your installed Zephyr SDK and dependencies.
Keep the application physically inside the workspace for source authorization:
a symlink to an outside checkout is not accepted. The example uses kernel APIs,
not board GPIO, and enables monitoring, names, stack information, initial stack
fill, stack sentinels, assertions, events and debug-friendly compilation. It
disables the FPU to exercise the supported basic Cortex-M exception frame.
It does not enable SMP, userspace or TrustZone.

At `validation_checkpoint`, after the first 50 ms sleep:

| Thread/object | Expected stable evidence |
| --- | --- |
| `validation_main` | Current, holding `validation_mutex` recursively twice |
| `validation_sem_thread` | Pending on `validation_sem`, count 0, limit 1 |
| `validation_mutex_thread` | Pending on the mutex owned by main |
| `validation_event_any` | Pending for any bit in `0x6` |
| `validation_event_all` | Pending for all bits in `0x6` |
| `validation_event` | Posted mask `0x1`, two waiters |
| `validation_sleeper` | Sleeping, with a touched local stack array |
| `validation_suspended` | Suspended after running, with a saved context |
| `idle` | Available between periodic main wakeups |

Waiters deliberately never complete. Main retains its locks and posted event
bits; no fixture-control commands or arbitrary target writes are necessary.
The checkpoint is in Thread mode, not inside a partially updated kernel queue.
The periodic main wakeups provide repeatable main/idle PendSV observations.
This is an inspection fixture, not a synchronization throughput benchmark.

## Opt-in physical acceptance

**Flashing is destructive to the board's existing application.** Reserve the
probe and preserve an appropriate recovery image first. Never run these tests
against another user's target. The test itself does not flash, erase or modify
option bytes. Flash explicitly with your normal tools, for example:

```console
pyocd list
pyocd flash -t stm32u5a5zjtxq -u PROBE_ID /path/to/zephyr-workspace/build-pygdb-rtos/zephyr/zephyr.hex
```

From the toolkit checkout, supply the matching build and a transport configuration
for that same probe:

```console
export PYGDB_RTOS_SERVER_CONFIG=/absolute/path/to/board.json
export PYGDB_RTOS_TYPE=zephyr
export PYGDB_RTOS_PROJECT=/path/to/zephyr-workspace/build-pygdb-rtos
export PYGDB_RTOS_WORKSPACE=/path/to/zephyr-workspace
tox -e rtos-hardware
```

This environment is **not** in the default Tox list. Outside it, the hardware test
is skipped unless `PYGDB_RTOS_HARDWARE=1` is explicitly supplied. Missing required
settings fail, rather than silently skipping an opted-in run.

The runner starts real pyGdbServer/GDB/OCD processes on dynamic loopback ports,
uses early MI async configuration and the test interpreter's Python dependencies,
and replaces GDB initialization with Arm selection, `monitor reset halt` and
accessible-memory setup. Other configured startup commands, including SVD
loading, are not executed. **It resets, halts and resumes the target**, places
temporary/internal breakpoints and stops/shuts down its helpers in `finally`.
It does not restore the previous firmware or guarantee post-disconnect run state.

The scenario verifies real client project transfer and artifact hashes, cached
display/status, audit identity, local sources and traversal rejection. On Zephyr
it checks all fixture threads, live/saved contexts, stack estimates, semaphore,
mutex and event waiters, and preservation of PC/SP plus selected GDB frame/thread
while inspecting from an older frame. It records exactly four PendSV samples,
checks the final pre-switch PC, automatic breakpoint cleanup, trace replacement
and cancellation on RTOS selection. MI running/stopped notifications synchronize
execution; it does not poll GDB Python commands while the inferior is running.

### Camelot regression on the same board

Use the already-built two-application Camelot sample, not the Zephyr artifacts.
The expected applications are `hello` and `sample-c-app`; the C application's
`handle_messages` function supplies a repeatable live-task breakpoint.

```console
pyocd flash -t stm32u5a5zjtxq -u PROBE_ID /path/to/sample-project/output/build/firmware.hex
export PYGDB_RTOS_TYPE=camelot
export PYGDB_RTOS_PROJECT=/path/to/sample-project
unset PYGDB_RTOS_WORKSPACE
tox -e rtos-hardware
```

Use the same explicit transport configuration. The scenario checks both tasks
and idle, live/saved stack/backtrace evidence, pending-event rendering, local C
sources and project metadata, then exactly four scheduler elections with cleanup.
It also verifies that Zephyr-only thread enumeration and separate-object commands
remain explicitly unavailable for Camelot.

No hardware scenario calls a cloud model or sends project sources off-host.
Provider tool routing, consent, masking, stale-project rejection and malformed
inputs remain covered by `tox -e client`, including offline SDK transports.
The preceding source-access step separately validated a real Copilot runtime
reading only five header lines of three known public Zephyr files. That is not
a live-model validation of the Camelot applications or an exhaustive audit.

## Recorded acceptance

On Nucleo U5A5, both opt-in scenarios passed using Zephyr
`v4.5.0-3564-ge9336422663` (declared 4.5.99) and the existing Camelot sample
declaring Sentry `v0.4.8`. The Zephyr fixture built with SDK 1.0.1; the host checks
ran on Python 3.13. Python 3.12 was unavailable on that host and remains an
unverified runtime for this physical acceptance, not a claimed hardware pass.
Build/version declarations do not establish repository cleanliness or source
revision equality. Repeat the scenarios with artifacts matching your own target.

The hardware fixture complements, rather than replaces, hardware-independent
tests for corrupted registries/queues, disabled observability, unsupported layouts,
ABI gates, context bounds, event masks, trace budgets and GDB lifecycle cleanup.
Run the relevant toolkit/server tests and the isolated client, lint, typecheck
and documentation Tox environments before publishing changes.
