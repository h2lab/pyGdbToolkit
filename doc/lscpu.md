# `lscpu` Command Technical Documentation

The `lscpu` command provides CPU identification and hardware electronic signature
inspection for ARM Cortex-M targets in GDB. Identification uses the Arm CPUID
and validated CoreSight debug ROM tables before applying manufacturer-specific
profiles. It is not restricted to STM32: the generic ROM discovery also exposes
manufacturer identity evidence on targets such as NXP devices.

---

## Technical Overview

When executed, `lscpu`:
1. Connects to GDB's active target memory via `TargetMemoryReader`.
2. Reads and decodes the Arm **CPUID Base Register**.
3. Discovers and validates the MCU and processor debug ROM tables, retaining their component identifiers and JEP106 manufacturer identities when available.
4. Queries manufacturer-specific device providers to identify the chip, electronic signatures, and factory memory configurations.
5. Renders a structured report using `Rich` console tables, or a generic Cortex-M report when no detailed device profile matches.

---

## Architecture and Internal Workflow

```
+-------------------+      1. Read CPUID (0xE000ED00)      +--------------------+
|  GDB Target /     | <----------------------------------- | TargetMemoryReader |
|  Cortex-M Target  |                                      +--------------------+
+-------------------+                                                |
          |                                                          v
          | 2. Validate debug ROM / CIDR / PIDR           +----------------------+
          +--------------------------------------------- | CoreSight discovery  |
                                                         +----------------------+
                                                                     |
                                                                     v
                                                         +----------------------+
                                                         | ProviderRegistry     |
                                                         | Manufacturer profile |
                                                         | or generic fallback  |
                                                         +----------------------+
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

### 2. Vendor Identification via Arm Debug ROM Tables

The Arm CPUID implementer identifies the CPU IP designer (for example Arm),
not necessarily the manufacturer of the surrounding MCU or SoC. Manufacturer
identification instead uses the validated debug ROM component identity:

- MCU ROM root at `0xE00FE000` and processor ROM root at `0xE00FF000`, when accessible and valid.
- Component ID registers (CIDR) to validate the CoreSight component and ROM-table class.
- Peripheral ID registers (PIDR) to obtain the component part number and the full JEP106 identity, including continuation bank and manufacturer code.
- The PIDR JEDEC-present indication before interpreting the identity as JEP106.

The MCU ROM and processor ROM identities must remain distinct: a processor ROM
can identify Arm while the MCU ROM identifies the chip manufacturer. Comparing
only the manufacturer code without its continuation bank is insufficient.
This architecture-level discovery is manufacturer-independent and supplies
evidence for selecting the appropriate device provider, rather than assuming
STM32 from the presence of a Cortex-M core.

For NXP targets, the same mechanism retains their ROM identity and component
part number without probing STM32-specific DBGMCU addresses. The toolkit also
includes NXP MCU and application-processor candidate memory-map profiles; see
[memmap.md](memmap.md). These are distinct from detailed `lscpu` device profiles
and are not proof of a particular chip model or physical memory capacity.

In the current provider registry, `Stm32Provider` supplies the detailed product
and electronic-signature decoding. A target without a matching registered
profile, including an NXP device not covered by such a profile, still receives
the generic Cortex-M report and its raw MCU ROM JEP106 identity. The `Vendor`
field then remains `Generic Cortex-M`; product line, RAM, Flash, package and
serial number are explicitly unavailable, not inferred from the CPU or ROM
part number. Additional manufacturer providers can consume the same validated
discovery data without changing the command.

#### STM32 Device Catalog (`Stm32Provider`)

For a matching STM32 ROM/device profile, the provider reads the corresponding
DBGMCU `IDCODE` and signatures. Catalog addresses include:
- `0x40015800` (STM32F0 / G0)
- `0xE0042000` (STM32F1, F2, F3, F4, F7, L0, L1, L4, G4, WB, WL)
- `0xE0044000` (STM32L5, U5)
- `0x44024000` (STM32H5)
- `0x5C001000` (STM32H7)
- `0x46001000` (STM32N6)

From the 32-bit `IDCODE` value, `DEV_ID` (`[11:0]`) and `REV_ID` (`[31:16]`) are extracted.

### 3. Electronic Signatures Reading

Once a manufacturer-specific device profile is matched, its documented
electronic-signature fields can be decoded. The current STM32 catalog provides:
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
