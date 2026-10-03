# Profiling & Peripheral Usage Analysis

The `profile` command ([implementation](https://github.com/h2lab/pyGdbToolkit/blob/main/src/pyGdbToolkit/cmd_profile.py)) scans target memory (Flash and SRAM) to detect active register address manipulations and determine which SVD peripherals are actively used by the firmware.

---

## Technical Overview

The profiling engine is designed to be fully generic and portable across any ARM Cortex-M or Cortex-R SoC. It relies on the active CMSIS-SVD hardware definitions and scans target memory for:

1. **32-bit Literal Pools & Pointer Tables**: Direct 32-bit words matching peripheral register addresses or base addresses.
2. **Thumb-2 MOVW + MOVT Immediate Loads**: Pairs of 16/32-bit instructions (`MOVW Rd, #imm16` + `MOVT Rd, #imm16`) reconstructing a 32-bit register target address.
3. **ARM 32-bit MOVW + MOVT Immediate Loads**: Standard 32-bit ARM instruction pairs reconstructing 32-bit register addresses.

---

## Command Reference

### `profile analyze flash [<start_address>] [<size>]`
Analyzes Flash memory (defaulting to the Flash base and size resolved from SVD / architecture) and renders a rich summary table of active and inactive peripherals.

```gdb
(gdb) profile analyze flash
(gdb) profile analyze flash 0x08000000 0x40000
```

### `profile analyze sram [<start_address>] [<size>]`
Analyzes SRAM memory (defaulting to the SRAM base and size resolved from SVD / architecture) and renders a rich summary table.

```gdb
(gdb) profile analyze sram
(gdb) profile analyze sram 0x20000000 0x10000
```

### `profile show [<peripheral>]`
Displays the cumulative list of peripherals identified as actively used across all previous analysis runs (e.g. Flash + SRAM scans). If a specific peripheral name is provided, displays the detailed breakdown of its accessed registers, offsets, hits count, and regions where they were found.

```gdb
(gdb) profile show
(gdb) profile show USART1
```

### `profile dump <output_file.json>`
Exports the accumulated profiling analysis metrics, scanned regions, and active peripherals dictionary to a JSON file.

```gdb
(gdb) profile dump /tmp/firmware_profile.json
```

### `profile help`
Displays the command syntax and reference table.
