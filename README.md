<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# pyGdbToolkit

`pyGdbToolkit` is a Python-based suite of GDB commands designed for inspecting, debugging, and auditing embedded ARM Cortex-M targets. Powered by `Rich`, it formats diagnostic output directly inside your GDB console.

---

## Quick Start and Usage in GDB

### Installation

Install `pyGdbToolkit` in your Python environment:

```bash
pip install .
```

### Loading in GDB

You can load `pyGdbToolkit` into GDB in two ways:

1. **Direct import in GDB session or `.gdbinit`**:

   ```text
   (gdb) python import pyGdbToolkit
   ```

2. **Using the provided [`gdbinit`](gdbinit) script**:

   ```bash
   gdb -x gdbinit
   ```

---

## Available Commands

| Command | Summary | Detailed Doc |
| :--- | :--- | :--- |
| **`lscpu`** | Displays ARM Cortex-M core details, DBGMCU identifiers, and factory electronic signatures (Flash size, 96-bit UID, package code). | [doc/lscpu.md](doc/lscpu.md) |
| **`fault_info`** | Performs automated fault diagnosis, decodes System Control Block (SCB) fault registers, and unstacks hardware exception frames from RAM (`MSP`/`PSP`). | [doc/fault_info.md](doc/fault_info.md) |
| **`secrethunt`** | Targeted entropy scanner for SRAM, Flash, and Backup registers to detect, classify, and export cryptographic keys (AES/ECC/RSA), key schedules, and secrets. | [doc/secrethunt.md](doc/secrethunt.md) |

---

## Command Overview

### `lscpu`

Displays CPU architecture details and vendor device electronic signatures:

```text
(gdb) lscpu
```

For technical architecture and internals, see [doc/lscpu.md](doc/lscpu.md).

### `fault_info`

Analyzes active Cortex-M hardware fault registers (`CFSR`, `HFSR`, `DFSR`, `MMFAR`, `BFAR`, `SFSR`, `SFAR`) and restores stacked exception registers (`R0`-`R12`, `LR`, `PC`, `xPSR`, `S0`-`S15`):

```text
(gdb) fault_info
```

For technical details and exception frame unstacking rules, see [doc/fault_info.md](doc/fault_info.md).

### `secrethunt`

Scans target memory for high Shannon entropy blocks, classifies candidate cryptographic secrets, and exports results to JSON:

```text
(gdb) secrethunt scan sram
(gdb) secrethunt classify --crypto
(gdb) secrethunt dump findings.json
```

For entropy algorithms, classification rules, and memory maps, see [doc/secrethunt.md](doc/secrethunt.md).

---

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
