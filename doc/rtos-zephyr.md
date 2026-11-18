<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# Zephyr RTOS support

The initial Zephyr backend loads and describes **single-image Arm Cortex-M
builds**. It supports both direct GDB loading and the pyGdbClient `project`
command. Live thread inspection and scheduler tracing are not implemented yet;
those commands fail explicitly rather than returning an empty thread list.

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

`rtos show task <name>` and `rtos showsched <num>` remain unavailable even if
the build enables debugging options. Thread enumeration, saved contexts,
stacks, synchronization and scheduler observation will be added incrementally.
