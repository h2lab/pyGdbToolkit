<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# pyGdbToolkit

Rich-based GDB commands for inspecting and diagnosing embedded debugging
targets, especially Arm Cortex-M microcontrollers.

## Why pyGdbToolkit?

Embedded debugging often requires switching between GDB, vendor tools, CMSIS
headers, SVD files, and ad-hoc scripts. pyGdbToolkit brings the most useful
inspection tasks into GDB itself and presents the results as readable Rich
tables.

It helps answer questions such as:

- Which processor and STM32 product line is connected?
- Which fault registers and stacked context explain a Cortex-M exception?
- What is the current call-stack layout, including NVIC and FPU frames?
- What are the live values and decoded bitfields of peripheral registers?
- Is the Cortex-M security configuration correctly set up?

The toolkit reads target memory through GDB, so it works with the GDB target
connection already used by the debugging session. It does not require a
separate probe protocol or vendor IDE.

## Requirements

- GDB with embedded Python support
- Python 3.12 or newer
- An Arm Cortex-M target for the Cortex-M-specific commands
- `requests` and `rich` (installed automatically with the package)

## Installation

Install the package in the Python environment used by GDB. From a clone of
this repository, an editable installation is convenient during development:

```console
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

For a regular installation, use `python -m pip install .` instead. The Python
interpreter used for installation must be compatible with GDB's embedded
Python interpreter. If GDB uses a different Python installation, install the
package with that interpreter or add the installed package directory to
`sys.path` in `gdbinit`.

## Loading the commands in GDB

The repository includes a [`gdbinit`](gdbinit) example that configures an Arm
target, connects to a local GDB server, resets the target, and loads the
package. Run GDB from the repository directory with:

```gdb
set architecture arm
target extended-remote localhost:3333
monitor reset halt
python
import subprocess,sys
paths = subprocess.check_output('python3 -c "import os,sys;print(os.linesep.join(sys.path).strip())"',shell=True).decode("utf-8").split()
sys.path.extend(paths)
end
py import pyGdbToolkit
```

The `py import pyGdbToolkit` line registers the commands provided by the
package. Change or comment out `target extended-remote localhost:3333` and
`monitor reset halt` when the target is configured by another tool or GDB
server. To use this file explicitly, start GDB with `gdb -x gdbinit`.

## Command reference

### `lscpu`

Displays the Arm CPU identity and, for supported STM32 devices, the
electronic signatures needed to identify the documented product line.

```
(gdb) lscpu
                                         Cortex-M CPU report

  Property                  Value
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Core type                 Cortex-M33
  Core revision             r0p4
  Implementer               Arm
  MCU ROM JEP106 identity   bank 0, code 0x20
  Vendor                    STMicroelectronics
  Product line              STM32U595/U599/U5A5/U5A9
  Part number               STM32U595/U599/U5A5/U5A9 (exact ordering code unavailable from MCU-ROM
                            part)
  RAM                       2512 KiB
  Flash                     4096 KiB
  Package type              0xF70C (raw package code; package type mapping unavailable)
  Serial number             0x000900263936500820313148 (96-bit UID)
```

`lscpu` reads the architected CPUID register and discovers the Cortex-M
CoreSight ROM table. It validates the component identity, uses the JEP106
vendor identity and component part number to select a documented STM32
profile, and reads only that profile's signature locations. It does not use
legacy DBGMCU identification registers or guess an exact ordering code.


### `fault_info`

Analyzes Cortex-M fault status registers and the stacked exception frame. It
decodes HardFault, MemManage, BusFault, UsageFault, and SecureFault details,
resolves stacked `PC` and `LR` addresses when symbols are available, and
reports likely causes such as invalid execution, null-pointer access, stack
overflow, divide-by-zero, or unaligned access.

```gdb
(gdb) fault_info
            ARM Cortex-M Fault Overview

  Property           Value
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Target Core        Cortex-M33 (r0p4) - Arm
  Active Exception   HardFault (Exception #3)
  Execution Mode     HANDLER (in exception)
  Current PC         0x080013FC [?]
  Current LR         0xFFFFFFB0 (valid EXC_RETURN)
  Current xPSR       0x69000003 (IPSR=3, T=1)

   Stacked frame at crash time (Stack: MSP @ 0x200017C8 | Return to:
            Handler (Secure) | Frame: Basic (8 registers))

  Register   Stacked Value   Details / Symbol
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  r0         0x00000002
  r1         0x20001804
  r2         0x00000000
  r3         0x20000134
  r12        0x200007E8
  lr         0x00000000      Caller: ?
  pc         0x00000000      <-- Faulting instruction: ?
  xpsr       0x6800000B      IPSR=11, T=0 (ARM (invalid on Cortex-M))

                            SCB (System Control Block) Status Registers

  Register    Value        Active Flags & Meaning
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  CFSR        0x00000101   IACCVIOL: MPU/security violation on instruction fetch (XN) (MemManage)
                           IBUSERR: Bus error on instruction fetch (BusFault)
   ├─ MMFSR   0x01         1 active flag(s)
   ├─ BFSR    0x01         1 active flag(s)
   └─ UFSR    0x0000       0 active flag(s)
  HFSR        0x40000000   FORCED: Fault escalated to HardFault (source handler disabled/masked)
  MMFAR       0x200017DC   [INVALID] Region: SRAM
  BFAR        0x200017DC   [INVALID] Region: SRAM
  DFSR        0x00000008   VCATCH: Vector catch triggered
  SHCSR       0x00050084   Enabled configurable handlers: MemManage, UsageFault
  VTOR        0x08000000   Vector table @ CODE (Flash / ROM)

                                    Diagnostics & Probable Causes
  • HardFault escalation: A BusFault was forced to HardFault (source handler disabled in SHCSR).
  • Instruction Fetch BusFault: Attempted execution from an invalid or inaccessible memory region
  (corrupted function pointer, overwritten vtable).
  • MPU violation on instruction: Attempted execution in an MPU region marked eXecute-Never (XN).   --Type <RET> for more, q to quit, c to continue without paging--

  • Crash location: Instruction at 0x00000000 (?), called from 0x00000000 (?).
```

The command accepts no arguments and is intended to be run after the target
has stopped in a fault handler.

`showstack` and `showstack dump` provide an overview. `showstack frame list`
lists consecutive frames, while `showstack frame <N>` gives a detailed report
for one frame. `showstack select` accepts `msp`, `psp`, or `auto` and affects
subsequent inspections.

### `svd`

Loads CMSIS-SVD descriptions and uses them to inspect, modify, monitor, and
export live peripheral register state.

```gdb
(gdb) svd load
(gdb) svd read /path/to/STM32F401.svd
(gdb) svd show GPIOA
(gdb) svd show GPIOA MODER
(gdb) svd write GPIOA MODER 0xA8000000
(gdb) svd monitor USART1 SR
(gdb) svd dump all /tmp/mcu-state.json
(gdb) svd list
```

- `svd load` detects the target and loads a matching SVD when possible.
- `svd read <file.svd>` loads a local SVD explicitly.
- `svd show <peripheral> [<register>]` reads a peripheral or decodes one
  register's bitfields.
- `svd write <peripheral> <register> <value>` writes and reads back a value.
- `svd monitor <peripheral> <register>` installs a write watchpoint and shows
  changed bitfields when it triggers.
- `svd dump <peripheral|all> <file.json>` exports a live JSON snapshot.
- `svd list` lists the loaded device peripherals.

Here is a typical example output:
```
(gdb) svd read STM32N657.svd
                        SVD File Loaded Successfully

  Property             Value
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  File Path            /path/to/STM32N657.svd
  Device Name          STM32N657
  Version              1.0
  Peripherals Loaded   248
```

Use `svd help` for the complete command syntax.

### `secscan`

Audits the security configuration of a Cortex-M-based SoC and produces a
clear security report. The audit covers generic Arm Cortex-M security
mechanisms and adds STM32-specific checks when the target is identified as an
STM32 device.

```gdb
(gdb) secscan audit
                                        secscan audit summary
  Core: Cortex-M33  |  Vendor: STMicroelectronics  |  Device: STM32U595/U599/U5A5/U5A9
  Generated: 2026-09-20T09:26:08+00:00
  FAIL: 1  WARN: 5  INFO: 6  PASS: 9

                                                 MPU

  Severity   Finding           Detail
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  FAIL       MPU is disabled   MPU_CTRL.ENABLE=0 while 8 region(s) are implemented; no memory
                               protection is active.

                                         CMSIS Core Security

  Severity   Finding                                Detail
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  WARN       Configurable fault handlers disabled   MemManage, BusFault, UsageFault disabled in
                                                    SHCSR; these faults will escalate to HardFault.
  WARN       Division-by-zero trap disabled         CCR.DIV_0_TRP=0: integer division by zero
                                                    silently returns 0 instead of faulting.
  INFO       Unaligned-access trap disabled         CCR.UNALIGN_TRP=0: unaligned accesses are
                                                    silently allowed except for LDM/STM/PUSH/POP.
  INFO       Debug access is currently enabled      DHCSR.C_DEBUGEN=1: ensure the debug port is
                                                    disabled/locked in production.

                                           TrustZone (SAU)

  Severity   Finding                Detail
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  INFO       TrustZone-M not used   SAU implements 0 regions and is disabled; this is likely a
                                    non-secure-only build.

                                        Fault Handlers (VTOR)

  Severity   Finding                                      Detail
 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  INFO       Vector table base                            VTOR=0x08000000.
  PASS       NMI handler is present                       0x080013FD.
  PASS       HardFault handler is present                 0x080013FD.
  PASS       MemManage handler is present                 0x080013FD.
  WARN       MemManage handler configured but not         SHCSR bit 16 is clear; faults will
             enabled                                      escalate to HardFault instead.
  PASS       BusFault handler is present                  0x080013FD.
  WARN       BusFault handler configured but not          SHCSR bit 17 is clear; faults will
             enabled                                      escalate to HardFault instead.
  PASS       UsageFault handler is present                0x080013FD.
  WARN       UsageFault handler configured but not        SHCSR bit 18 is clear; faults will
             enabled                                      escalate to HardFault instead.
  PASS       SVCall handler is present                    0x080013FD.
  PASS       PendSV handler is present                    0x080013FD.
  PASS       SysTick handler is present                   0x080013FD.
  [...]
(gdb) secscan report /tmp/security-report.json
```

- `secscan audit` analyzes the MPU configuration, checks that regions are
  valid and non-overlapping, and verifies suitable write/read/execute
  permissions. It also checks that stacks and RAM are non-executable and that
  executable Flash is not writable.
- The audit checks the security-related CMSIS peripheral mechanisms available
  on the target and analyzes whether Arm TrustZone is enabled and correctly
  configured.
- It verifies the VTOR and critical exception handlers, including UsageFault
  and MemManage, and checks that the core fault mechanisms are present.
- On STM32 targets, it also checks the device's read-out protection (RDP)
  state.
- `secscan report <report.json>` writes a clear report of the security state
  to a JSON file.

## More documentation

The [`doc/`](doc/) directory contains deeper technical documentation:

- [`fault_info`](doc/fault_info.md)
- [`lscpu`](doc/lscpu.md)
- [`secscan`](secscan.md)
- [`svd`](doc/svd.md)

## STM32 `lscpu` metadata

`lscpu` reads CPUID, then discovers the memory-mapped MCU CoreSight ROM table
at `0xE00FE000`. It validates the root Component and Peripheral ID registers,
uses the full JEP106 bank/code identity and 12-bit component part to select a
vendor profile, and reads only the selected profile's documented electronic
signature locations. It also scans the processor ROM table at `0xE00FF000` as
best-effort diagnostics; this processor topology is never used for vendor
selection. No Debug Port, MEM-AP, or vendor debug-identification register is
required or read. In particular, the strict no-DBGMCU policy has no legacy
register fallback.

An unknown, invalid, or inaccessible MCU ROM root, or an ST part not in the
MCU-ROM mapping, produces a generic Cortex-M report instead of guessing from
legacy device identifiers. The local signature catalog is derived from ST's
official CMSIS device headers (addresses, fixed SRAM blocks, and `FLASH_SIZE`
fallback semantics); headers are not vendored:

- [`cmsis-device-c0`](https://github.com/STMicroelectronics/cmsis-device-c0),
  [`cmsis-device-f0`](https://github.com/STMicroelectronics/cmsis-device-f0),
  [`cmsis-device-f1`](https://github.com/STMicroelectronics/cmsis-device-f1),
  [`cmsis-device-f2`](https://github.com/STMicroelectronics/cmsis-device-f2),
  [`cmsis-device-f3`](https://github.com/STMicroelectronics/cmsis-device-f3),
  [`cmsis-device-f4`](https://github.com/STMicroelectronics/cmsis-device-f4), and
  [`cmsis-device-f7`](https://github.com/STMicroelectronics/cmsis-device-f7)
- [`cmsis-device-g0`](https://github.com/STMicroelectronics/cmsis-device-g0),
  [`cmsis-device-g4`](https://github.com/STMicroelectronics/cmsis-device-g4),
  [`cmsis-device-l0`](https://github.com/STMicroelectronics/cmsis-device-l0),
  [`cmsis-device-l1`](https://github.com/STMicroelectronics/cmsis-device-l1),
  [`cmsis-device-l4`](https://github.com/STMicroelectronics/cmsis-device-l4),
  [`cmsis-device-l5`](https://github.com/STMicroelectronics/cmsis-device-l5),
  [`cmsis-device-u0`](https://github.com/STMicroelectronics/cmsis-device-u0),
  [`cmsis-device-u5`](https://github.com/STMicroelectronics/cmsis-device-u5),
  [`cmsis-device-wb`](https://github.com/STMicroelectronics/cmsis-device-wb),
  [`cmsis-device-wba`](https://github.com/STMicroelectronics/cmsis-device-wba), and
  [`cmsis-device-wl`](https://github.com/STMicroelectronics/cmsis-device-wl)
- [H5](https://github.com/STMicroelectronics/cmsis-device-h5),
  [H7](https://github.com/STMicroelectronics/cmsis-device-h7),
  [H7RS](https://github.com/STMicroelectronics/cmsis-device-h7rs), and
  [U3](https://github.com/STMicroelectronics/cmsis-device-u3)
- [C5 Device Family Pack](https://github.com/STMicroelectronics/stm32c5xx-dfp)
  and [N6 CMSIS headers](https://github.com/STMicroelectronics/cmsis-device-n6)

The MCU-ROM component part identifies a documented product line, not an
orderable SKU. `lscpu` therefore never invents an exact ordering code. Where ST
does not publish a family-specific package-code-to-package-name mapping, it
prints the raw package code and explicitly says that the package type cannot be
mapped.
