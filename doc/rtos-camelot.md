<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# Camelot RTOS support

This page describes the current `rtos select camelot` implementation. For the
GDB commands and a user workflow, see [`rtos.md`](rtos.md). Camelot is the
only RTOS supported at present, but project-specific ELF discovery, metadata
decoding, and task inspection live in its own module so that other RTOS
implementations can be added separately.

## Project inputs

`rtos load-project --from <path>` takes a built Camelot project root. It reads
the following files relative to that directory:

| File | Use |
| --- | --- |
| `project.toml` | Find the `[kernel]` block and `[application.<name>]` blocks |
| `output/build/camelot_private/sentry-kernel.elf` | Kernel symbols and `.task_list` section |
| `output/build/camelot_private/<name>-app.elf` | Symbols for each declared application |
| `output/build/camelot_private/layout.json` | Linked text and RAM mapping for kernel and tasks |
| `output/build/kernel/kernel/include/sentry/managers/task_metadata.h` | Generated `task_meta_t` definition for this build |
| `output/build/kernel/subprojects/kconfig-*/generated_kconfig.json` | Stack layout setting for live inspection |

The `[runtime]` block does not add an ELF. Neither
`<name>-app.dummy.elf` nor other dummy link products are loaded. Each expected
linked ELF must exist; the symbols and metadata must correspond to the image
on the target when using live commands.

## Task metadata and mapping

The kernel ELF's `.task_list` section has a linked start address and byte
size. GDB obtains the size and field offsets of `task_meta_t` from the
kernel ELF's DWARF information. The section size must be a multiple of this
type's size; their quotient gives the number of slots. Nonempty slots are
decoded using the ELF's byte order. Each slot retains its index, address,
all metadata fields, and task identity.

The metadata `s_text` address is matched to a non-kernel text region in
`layout.json`, and `s_svcexchange` is checked against that task's RAM region.
An unmatched populated slot is an error rather than a guessed task name.
Empty slots remain in the context but are not shown as applications. The
kernel's `idle` task can appear in the layout even when it has no task-list
slot. Addresses displayed by `rtos show` use end-exclusive ranges.

For the calculator build, for example, `rtos show` displays `kernel`, `idle`,
and `calculator-app`. The populated `.task_list` slot maps to
`calculator-app`; the idle mapping is separate.

## Live `task_table` inspection

The running kernel owns `task_t task_table[CONFIG_MAX_TASKS+1]`. After the
target is halted, `rtos show task <taskname>` uses GDB's `task_t` and
`job_state_t` debug types to inspect it. A task is identified by the pointer
to its `.task_list` slot in `task_t.metadata`; `idle` is identified by its
reserved label `0xCAFE`. Source indexes in `ipcs[]` and `sigs[]` refer to
entries in this live table. Only nonzero entries represent pending IPCs or
signals. The `ints[]` and optional `dmas[]` queues are read between their
bottom and head indexes, including wraparound. When an SVD has been loaded,
IRQ numbers are resolved using its `<interrupt>` entries; without it, the
numbers remain available.

The stack bounds use the task's live metadata sizes and the generated
`CONFIG_SVC_EXCHANGE_AREA_LEN`, with the kernel's 4-byte alignment. Stack
usage is the distance between the stack top and the saved SP, or the live PSP
when execution is stopped inside the task. It is not a maximum historical
usage estimate. GDB can unwind the currently executing task; for an inactive
task the saved `stack_frame_t` supplies PC and LR candidates, symbolized only
if they lie in that task's text region. An exception-return LR such as
`0xFFFFFFBC` is not an application symbol, and a complete inactive-task
backtrace is not reconstructed.

## Election tracking

`rtos showsched <num>` places a GDB breakpoint on the kernel's
`sched_elect()` symbol. A finish breakpoint samples the elected task handle
on return, then maps that handle through the live `task_table` to a task
label and name; the idle label is handled separately. On the requested
election, tracing stops and the Rich chart is printed. The trace reads a live
target, unlike `rtos show`, which reads the built layout and metadata.