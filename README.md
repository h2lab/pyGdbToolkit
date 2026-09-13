<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# pyGdbToolkit

Rich-based GDB commands for inspecting embedded debugging targets.

## STM32 `lscpu` metadata

`lscpu` identifies STM32 Cortex-M product lines from `DBGMCU_IDCODE` and reads
only their documented electronic-signature locations.  The local catalog is
derived from ST's official CMSIS device headers (addresses, fixed SRAM blocks,
and `FLASH_SIZE` fallback semantics); headers are not vendored:

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

The DEV_ID and revision identify a documented product line, not an orderable
SKU. `lscpu` therefore prints the narrowest supported line plus raw DEV_ID and
REV_ID, never inventing an exact ordering code. Where ST does not publish a
family-specific package-code-to-package-name mapping, it prints the raw package
code and explicitly says that the package type cannot be mapped.
