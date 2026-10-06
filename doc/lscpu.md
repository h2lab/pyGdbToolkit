<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# `lscpu` Command Technical Documentation

The `lscpu` command provides CPU identification for ARM Cortex-M and AArch64
targets in GDB. Each architecture has its own collector, report model, and renderer:

- **ARM / Cortex-M**: CPUID and validated CoreSight debug ROM tables provide CPU
    and manufacturer identity before device-specific electronic-signature profiles
    are applied. Support is not restricted to STM32; ROM identity can also identify
    manufacturers such as NXP.
- **AArch64 / ARMv8-A**: Architected system registers provide CPU identity,
    affinity, available features, and cache information without SoC-specific memory
    addresses or Cortex-M ROM-table discovery.

---

## Technical Overview

When executed, `lscpu`:
1. Rejects arguments and calls the common `render_report()` entry point.
2. Obtains `SESSION.architecture` from the session's cached architecture probe.
    AArch64 detection uses the bound inferior's GDB architecture metadata;
    Cortex-M detection uses CPUID through `TargetMemoryReader`.
3. Dispatches explicitly with `match SESSION.architecture`: `Architecture.ARM`
    selects the Cortex-M path and `Architecture.AARCH64` selects the AArch64 path.
4. Collects the architecture-specific report and passes it to its Rich renderer.
5. Raises `gdb.GdbError` when the architecture is unknown or unsupported. ARM is
    not a fallback for other architectures.

| Architecture | Collector | Main evidence | Report | Renderer |
|---|---|---|---|---|
| ARM / Cortex-M | `device_report()` | CPUID, validated ROM identities, device profiles | `DeviceReport` | `render_arm_report()` |
| AArch64 / ARMv8-A | `cpu_report()` | Named system registers, verified J-Link aliases or OpenOCD external MIDR | `CpuReport` | `render_aarch64_report()` |

The generic Cortex-M device report is a fallback **within the ARM provider
registry** when no detailed device profile matches. It is not an architecture
fallback and is never used for an AArch64 target.

---

## GDB Usage

```text
(gdb) lscpu
```

The same command is used for both supported architectures. It accepts no
arguments and raises `gdb.GdbError` for invalid arguments or an unavailable or
unsupported architecture.

---

## Architecture and Internal Workflow

```text
lscpu
    |
    v
render_report()
    |
    v
match SESSION.architecture
    |
    +-- Architecture.ARM
    |     |
    |     v
    |   device_report()
    |     -> CPUID / Cortex-M identity
    |     -> CoreSight ROM-table discovery
    |     -> ProviderRegistry / device profile or generic Cortex-M report
    |     -> DeviceReport
    |     -> render_arm_report()
    |
    +-- Architecture.AARCH64
    |     |
    |     v
    |   cpu_report()
    |     -> require AArch64TargetDescription
    |     -> collect_cpu_report() / GdbCpuRegisterReader
    |     -> named GDB registers / J-Link aliases / OpenOCD external MIDR
    |     -> CpuReport / MIDR identity and register availability
    |     -> render_aarch64_report()
    |
    +-- None / unsupported architecture
                -> gdb.GdbError (no ARM fallback)
```

---

## ARM / Cortex-M

The following memory-access, ROM-discovery, and device-provider steps apply only
to the ARM Cortex-M path. They are not executed for an AArch64 target.

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

The provider registry includes `Stm32Provider` for detailed product/signature
decoding and `NxpProvider` for manufacturer recognition without additional
target reads. NXP identification requires a valid MCU ROM root at `0xE00FE000`
with JEP106 bank `0`, code `0x15` (NXP/Philips) or code `0x0E`
(legacy Freescale/Motorola). The latter is normalized to `NXP Semiconductors`,
while the original ROM identity remains in the report. A processor ROM used
as a discovery fallback is not accepted as SoC manufacturer evidence.

`NxpProvider` sets `Vendor` to `NXP Semiconductors` but leaves product line,
ordering code, RAM, Flash, package and serial number unavailable: no documented
NXP device-specific signature profile is currently registered. It does not read
OTP, peripheral registers or factory signatures. A ROM component part number
is not treated as a unique chip model.

For the i.MX8MP companion M7, CPUID identifies Cortex-M7 independently of the
OCD logs. However, a ROM identity of bank `4`, code `0x3B` identifies **Arm**,
not NXP; this evidence alone cannot identify the i.MX8MP SoC. Such a target
still uses the generic report unless it exposes a recognized manufacturer ROM.
The provider does not infer NXP from a configured J-Link device or from the CPU
type. Manufacturer recognition and J-Link `dap core` connection correlation
are separate concerns; see [dap.md](dap.md).

Other unmatched identities retain the generic Cortex-M report and raw ROM
identity, with unavailable fields rather than guessed capacities. Additional
manufacturer providers can consume the validated discovery without changing
the command.

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

`render_arm_report()` formats the `DeviceReport` in a Rich table
(`box.SIMPLE_HEAVY`) titled `Cortex-M CPU report`. Property names are bold;
unavailable or unreadable fields explicitly display yellow
`Unavailable: <reason>` status indicators.

---

## AArch64 / ARMv8-A

For a target reported by GDB as `aarch64` (or `aarch64:ilp32`), `lscpu` uses a
separate architectural CPU report. It does not read Cortex-M SCB or ROM-table
addresses, use a configured device name as CPU identity, or probe SoC peripherals.
If the server advertises an ambiguous name such as `armv8-a`, configure
`set architecture aarch64` in GDB before using the toolkit.

### Execution workflow

1. The common `render_report()` dispatcher selects `Architecture.AARCH64` from
    `SESSION.architecture` and calls `cpu_report()`.
2. `cpu_report()` requires the session's `AArch64TargetDescription` and creates a
    `GdbCpuRegisterReader`. It does not invoke the ARM device-provider registry.
3. `collect_cpu_report()` requests the standard named registers through the GDB
    adapter. Named access is preferred; a positively identified backend can use
    the verified J-Link aliases or OpenOCD external MIDR access described below.
    `CurrentEL` can also be derived
    from GDB PSTATE when its execution-state bit confirms AArch64.
4. The collector preserves each register's value, access source, valid bit width,
    and availability in a `CpuReport`. It decodes CPU identity from `MIDR_EL1`
    when available, without changing the session's metadata-only target description.
5. `render_aarch64_report()` renders CPU identity, available affinity and features,
    cache line sizes, and raw register evidence in an `AArch64 CPU report` Rich table.
    Missing registers remain unavailable and unread high bits are shown as `?`.

Register collection is fresh on every invocation, unlike the cached ARM device
report. A missing system register does not redirect execution to the Cortex-M
path or imply that the corresponding CPU feature is absent.

### System-register collection and decoding

The architecture-independent GDB register interface is preferred. The collector
requests the standard system-register names in lowercase and architected spelling:

| Register | Information |
|---|---|
| `MIDR_EL1` | CPU implementer, part, variant and revision |
| `MPIDR_EL1` | Affinity levels `Aff3:Aff2:Aff1:Aff0`, MT and U bits |
| `REVIDR_EL1` | Raw implementation-defined revision information |
| `ID_AA64PFR0_EL1` | Raw processor features; FP and Advanced SIMD decoding |
| `ID_AA64ISAR0_EL1` | Raw instruction-set feature information |
| `ID_AA64MMFR0_EL1` | Raw memory-model features; physical address width |
| `ID_AA64DFR0_EL1` | Raw debug feature information |
| `CTR_EL0` | Minimum instruction and data cache line sizes |
| `DCZID_EL0` | Raw data-cache zero information |
| `CurrentEL` | Current exception level, also obtainable from AArch64 PSTATE |

The decoded CPU implementer is the IP designer, not the SoC manufacturer. Known
Arm part numbers identify cores such as Cortex-A53 and Cortex-A72. Unknown
implementer/part pairs remain explicit; no i.MX8MP identification is inferred.
Physical address width is an architectural capability, not a RAM-size measurement.
MPIDR affinity describes the selected processing element, not a system-wide CPU
count. Registers are collected afresh on each invocation.

### Limited J-Link fallback

SEGGER J-Link GDB Server V9.82 does not expose the A53 system registers through
its GDB target register description. When J-Link is positively identified and a
named GDB register is unavailable, the adapter permits only these verified
read-only commands, using AArch64 system-register encoding components:

| Register | Command | Available bits |
|---|---|---|
| `MIDR_EL1` | `monitor cp15 0,0,0,0` | Low 32 bits; high bits are architectural `RES0` |
| `REVIDR_EL1` | `monitor cp15 0,0,0,6` | Low 32 bits only |
| `CTR_EL0` | `monitor cp15 0,0,3,1` | Low 32 bits only |

The argument order is `CRn, CRm, op1, op2`. In particular, the AArch64 `CTR_EL0`
encoding uses `op1=3`; the AArch32 CTR encoding is not interchangeable here.
The fallback is never sent to OpenOCD, pyOCD, or an unknown server. Responses
must identify the requested encoding and contain a 32-bit value.

Every raw register includes its access source. Unknown high bits from partial
reads are displayed as `????????`, not zero-filled. Other unavailable registers
remain explicitly unavailable, without implying the CPU lacks those features.
The toolkit does not inject instructions, change cache selectors, reset the
target, or scan memory to obtain the report. The debug server may halt the CPU
and perform its own internal register-access sequence.

### OpenOCD external CPU identification

OpenOCD 0.12.0 exposes the A53 general registers and PSTATE to GDB, but not
MIDR as a named GDB register. Its `aarch64 mrc` command rejects an AArch64
execution state; the toolkit does not use it or inject its own MRS sequence.
Instead, MIDR can be read from the architectural external debug interface:
`Debug base + 0xD00` contains the low 32 bits of `MIDR_EL1`, whose high bits
are architectural `RES0`. The offset belongs to the AArch64 model, not a SoC
address table.

The OpenOCD adapter requires the selected GDB hardware-thread name to match
`target current`. It queries the CPU's configured `-type`, `-dbgbase`, `-dap`
and `-ap-num`, then looks for exactly one configured `mem_ap` target on the same
DAP and AP. Only target metadata is enumerated; no AP or ROM-table scan occurs.
It reads one 32-bit word with the target-specific `read_memory` command and
retains the source `OpenOCD external debug MIDR`. The
[OpenOCD A53 example](configuration-examples.md) provides the required APB
view with its GDB port disabled.

Missing/ambiguous views, unsafe names, a mismatched monitor context, read errors
or malformed results leave MIDR unavailable. No target is created dynamically,
no debug base is guessed, and no virtual or physical system-memory read is
used as a substitute. The MEM-AP read still changes debug transfer registers
internally and is not guaranteed harmless on powered-down or locked components.
MPIDR, REVIDR, CTR and AA64 feature registers not exposed by this OpenOCD build
remain unavailable; external feature registers are not treated as identical
full `ID_AA64*` values.

### Hardware verification

On 2026-10-06, the attached i.MX8MP A53 was tested with J-Link V9.82 and
gdb-multiarch 16.3, using JTAG at 1000 kHz and `-noreset -noir`. The command
reported Cortex-A53 r0p4 from `MIDR_EL1=0x410FD034`, Arm as CPU implementer,
EL1 from PSTATE, and 64-byte minimum instruction/data cache lines from
`CTR_EL0[31:0]=0x84448004`. `REVIDR_EL1[31:0]` was `0x00000380`.
MPIDR and the AA64 feature registers remained unavailable through that server.
This is CPU-report validation, not SMP or SoC-signature validation.

The same four A53s were verified through OpenOCD 0.12.0 using the J-Link JTAG
adapter at 1000 kHz. `lscpu` identified Cortex-A53 r0p4 from external MIDR
`0x410FD034` and EL1 from PSTATE after repeated core pivots, in standalone GDB
and through the real pyGdbServer WebSocket API. Unlike the tested SEGGER path,
cache-line and implementation revision fields remained unavailable. See
[SMP support](smp.md) for the parallel context models and validation limits.

Register field definitions follow the Arm A-profile system-register specification:
[MIDR_EL1](https://df.lth.se/~getz/ARM/SysReg/AArch64-midr_el1.html),
[MPIDR_EL1](https://df.lth.se/~getz/ARM/SysReg/AArch64-mpidr_el1.html),
[ID_AA64PFR0_EL1](https://df.lth.se/~getz/ARM/SysReg/AArch64-id_aa64pfr0_el1.html),
[ID_AA64MMFR0_EL1](https://df.lth.se/~getz/ARM/SysReg/AArch64-id_aa64mmfr0_el1.html),
[CTR_EL0](https://df.lth.se/~getz/ARM/SysReg/AArch64-ctr_el0.html).
The [external MIDR definition](https://df.lth.se/~getz/ARM/SysReg/ext-midr_el1.html)
specifies the `0xD00` debug-component offset used by the OpenOCD identity path.
