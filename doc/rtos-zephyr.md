<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# Zephyr RTOS support

The initial Zephyr backend loads and describes **single-image Arm Cortex-M
builds**. It supports both direct GDB loading and the pyGdbClient `project`
command. Live thread identity/state inspection supports Zephyr **4.1 through
4.x**, using DWARF field names rather than fixed structure offsets. Supported
Cortex-M builds also expose core registers, stack measurements and backtrace
evidence. Scheduler tracing is not implemented yet.

## Build inputs

Use the **build directory**, not the application source directory or the West
workspace root. These exact paths are required relative to it:

| File | Use |
| --- | --- |
| `build_info.yml` | Application, board, qualifiers and Zephyr version |
| `zephyr/zephyr.elf` | Final linked image, symbols, embedded DWARF and memory segments |
| `zephyr/.config` | Effective configuration and available observability options |
| `zephyr/zephyr.dts` | Build's generated devicetree, retained as an audit artifact |

Currently `build_info.yml` schema `0.1.0` and a linked ELF32 little-endian Arm
image with a defined `_kernel` symbol and embedded DWARF are required. The board
must agree between the metadata and configuration; `CONFIG_ARCH="arm"` and
`CONFIG_CPU_CORTEX_M=y` must identify a supported build. Multi-image/sysbuild
roots and other architectures are not supported at this stage.

Only the final `zephyr.elf` is loaded, never `zephyr_pre0.elf` or other pre-link
images. Loading does not run CMake, West, build scripts or pickle files. The
devicetree is transferred and hashed but is not decoded for live inspection.
Install the package dependencies, including PyYAML and pyelftools, in an
environment accessible to GDB's embedded Python; see
[installation](getting-started.md).

## Direct GDB workflow

```gdb
(gdb) py import pyGdbToolkit
(gdb) rtos select zephyr
(gdb) rtos load-project --from /path/to/zephyr-workspace/build
(gdb) rtos show
```

No connected target is needed. `rtos show` displays cached application and board
identity, the Zephyr version, ELF segments with end-exclusive runtime ranges,
load addresses, stored byte counts and ELF flags, plus selected Kconfig options.
These are **build-time attributes**, not measured MPU permissions or physical
memory capacities. Zephyr threads share a linked image; the displayed segments
are not private text/RAM allocations for individual threads.

The symbols remain available to ordinary GDB commands. Loading does not flash,
reset, resume or read the target. For later live debugging, use artifacts that
match the firmware actually running on the board.

## Live threads

Connect and stop all GDB threads of the selected target before using:

```gdb
(gdb) rtos show threads
(gdb) rtos show task worker
(gdb) rtos show task 0x200000A8
```

The list displays the `k_thread` address, available name, signed priority,
decoded/raw state bits and current/idle CPU roles. Single-thread inspection
also shows raw user options and, with monitoring enabled, the entry address.
Current means scheduled by the kernel: an interrupt may actually be executing.
Combined flags such as pending/sleeping are preserved, not flattened into a
single guessed state.

With `CONFIG_THREAD_MONITOR=y`, enumeration follows `_kernel.threads` through
`k_thread.next_thread`. Cycles, misaligned/null thread identifiers, out-of-range
addresses, unreadable memory and more than 4096 monitored entries fail explicitly;
they never produce a truncated healthy-looking list. CPU references supplement
the list, with a warning if a referenced thread is absent from the registry.
Field and type lookup is anchored to the loaded project's ELF and DWARF
compilation unit, not an arbitrary previously loaded image.

Without monitoring, the list is explicitly **incomplete**: only current/idle
CPU references are discoverable. You can inspect another `k_thread` by its
hexadecimal address, but membership in the kernel registry cannot be verified.
Neither ready queues nor static initialization records are mistaken for an
exhaustive live thread registry.

Names require `CONFIG_THREAD_NAME=y`. Disabled or unrecorded options are labeled
as unavailable; an enabled but empty name is shown as unnamed. Exact names may
select a thread only when unique. If names collide, use the displayed address.
Identifiers are never evaluated as GDB expressions.

To improve a **future matching firmware build**, enable
`CONFIG_DEBUG_THREAD_INFO=y` (which selects thread monitoring and naming in the
current Zephyr tree), or explicitly enable monitoring and naming as appropriate
to your Zephyr configuration. Loading project artifacts does not change firmware
or enable these options. Releases before 4.1 and unrecognized major versions keep
build loading but disable live inspection: state-bit meanings must not be guessed
across kernel ABIs.

## Stack and core-register context

`rtos show task <name-or-address>` also inspects the selected thread's stack
when `CONFIG_THREAD_STACK_INFO=y`. Bounds come from `stack_info.start/size`;
the initial SP is `start + size - delta`, excluding TLS and randomized top
storage. Misaligned, wrapping or inconsistent bounds, pointers outside them
and exception frames that do not fit are errors, never guessed usage figures.
Dummy and terminated threads have no reported resumable context.

For the **current thread**, on a single-CPU kernel stopped in Thread mode, the
command reads the **newest** GDB frame's core registers and walks its backtrace
(at most 64 frames). It does not depend on the older frame a user may have
selected. In Handler mode the current thread's saved context may be stale:
register/backtrace recovery is explicitly unavailable rather than attributing
ISR registers to the thread. Current-thread register recovery is also unavailable
when the kernel has multiple CPU slots or SMP, since the GDB-to-Zephyr CPU mapping
has not been established.

For an **inactive thread**, R4-R11 and PSP come from `callee_saved` through
DWARF. The saved PSP points to Zephyr's eight-word basic exception frame;
PC/LR and caller-saved core registers are decoded there. Saved EXC_RETURN
distinguishes basic versus FP-extended storage (32 versus 104 bytes), and
xPSR's alignment flag adds four bytes when present. The reconstructed SP is
shown separately from the saved frame base. FP register values themselves
are not reported. An FPU build must record EXC_RETURN, and an extended frame
must agree with FPU/FPU-sharing configuration.

Saved PC and LR are symbolized as **evidence**, not presented as an invented
complete backtrace. Full unwinding of an inactive call chain is not available.
This is the same distinction made for inactive Camelot tasks.

SP-based occupancy is an instantaneous measurement, **not a high-water mark**.
With `CONFIG_INIT_STACKS=y`, a separate scan estimates touched space from the
initial `0xAA` fill. It excludes top `delta` storage and, when configured, the
four-byte stack sentinel, reads at most 256 bytes per request, and refuses scans
larger than one MiB. A fill pattern can occur in real data, so this is labeled
an estimate, not proof of peak use or overflow safety. With INIT_STACKS disabled
or absent, the watermark is explicitly unavailable; no fill scan is attempted.

The current decoder supports the ordinary downward-growing Cortex-M PendSV
layout. USE_SWITCH, userspace/privileged dual stacks, TrustZone firmware variants
and upward-growing stacks retain thread identity inspection but explicitly
report stack/context unavailability. Unknown exception-return modes are rejected.
Commands never write target memory/registers, switch GDB threads/frames, resume
the target or install an unwinder. Target read failures remain errors.

## Remote client and audit context

In the pyGdbClient dashboard:

```text
project /path/to/zephyr-workspace/build zephyr
audit
```

The path belongs to the **client**. The shared provider first parses
`build_info.yml`, then the client uploads exactly the four required files through
the existing RTOS transfer protocol. The server stages them and invokes the
same GDB loader; no shared filesystem is required.

The audit context records the application and kernel components, declared
Zephyr version, and size/SHA-256 of every transferred artifact. The application
version and kernel repository revision are not inferred from the Zephyr version:
the latter is a declared version string, not a verified Git commit.

Absolute application, Zephyr and module paths in build metadata do **not**
authorize source reads. At this stage the components have no authorized source
tree, so Zephyr application/kernel/module source access by the LLM is not yet
available. Explicit multi-root source authorization is a separate stack step;
artifact loading must not silently grant access outside the selected build.

## Current runtime limits

The version 1 `rtos.status` snapshot reports `selected_rtos="zephyr"`,
`loaded=true` after successful loading, and `task_list=null`: Zephyr has no
Camelot-style static task slots. Status and build display use cached data and
do not enumerate live threads or reread the filesystem.

Thread samples are obtained only by the explicit live commands, not by status
or build display. Wait-object resolution, synchronization and scheduler
observation remain later steps. `rtos showsched <num>` remains
unavailable even if the build enables debugging options.
