# `lscpu` Command Technical Documentation

The `lscpu` command provides detailed CPU identification and hardware electronic signature inspection for ARM Cortex-M targets in GDB, with rich vendor-specific decoding for STM32 product lines.

---

## Technical Overview

When executed, `lscpu`:
1. Connects to GDB's active target memory via `TargetMemoryReader`.
2. Reads and decodes the Arm **CPUID Base Register**.
3. Queries vendor-specific device providers to identify the chip, electronic signatures, and factory memory configurations.
4. Renders a structured report using `Rich` console tables.

---

## Architecture and Internal Workflow

```
+-------------------+      1. Read CPUID (0xE000ED00)      +--------------------+
|  GDB Target /     | <----------------------------------- | TargetMemoryReader |
|  Cortex-M Target  |                                      +--------------------+
+-------------------+                                                |
          |                                                          v
          | 2. Read DBGMCU IDCODE & Signatures             +--------------------+
          +----------------------------------------------- | Stm32Provider      |
                                                           +--------------------+
                                                                     |
                                                                     v
                                                           +--------------------+
                                                           | DeviceReport       |
                                                           +--------------------+
                                                                     |
                                                                     v
                                                           +--------------------+
                                                           | Rich Table Render  |
                                                           +--------------------+
```

### 1. CPUID Base Register Decoding (`0xE000ED00`)

The command reads 32 bits from address `0xE000ED00` and extracts:
- **Implementer**: `[31:24]` (e.g., `0x41` -> Arm).
- **Variant**: `[23:20]` (e.g., `r0`, `r1`).
- **Architecture**: `[19:16]` (e.g., `0xF` -> ARMv7-M / ARMv8-M).
- **Part Number**: `[15:4]` (e.g., `0xC23` -> Cortex-M3, `0xC24` -> Cortex-M4, `0xD21` -> Cortex-M33).
- **Revision**: `[3:0]` (e.g., `p0`, `p1`).

### 2. Vendor Identification & Device Catalog (`Stm32Provider`)

For STM32 devices, `lscpu` probes known DBGMCU `IDCODE` base addresses in sequence:
- `0x40015800` (STM32F0 / G0)
- `0xE0042000` (STM32F1, F2, F3, F4, F7, L0, L1, L4, G4, WB, WL)
- `0xE0044000` (STM32L5, U5)
- `0x44024000` (STM32H5)
- `0x5C001000` (STM32H7)
- `0x46001000` (STM32N6)

From the 32-bit `IDCODE` value, `DEV_ID` (`[11:0]`) and `REV_ID` (`[31:16]`) are extracted.

### 3. Electronic Signatures Reading

Once the device profile is matched against the STM32 catalog:
- **Flash Size Register**: Reads the factory-encoded Flash size in KiB.
- **Unique Device ID (UID)**: Reads the 96-bit (12-byte) factory UID.
- **Package Code Register**: Decodes package type if documented for the product line.
- **RAM Total**: Reports factory RAM size associated with the product line.

### 4. Rendering with `Rich`

The results are formatted in a `Rich` table (`box.SIMPLE_HEAVY`):
- Available fields are shown in bold text.
- Unavailable or unreadable fields explicitly display yellow "Unavailable: <reason>" status indicators.

---

## GDB Usage

```text
(gdb) lscpu
```

### Options
- Accepts no arguments. Returns `gdb.GdbError` if arguments are provided.
