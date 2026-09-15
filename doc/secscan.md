# `secscan` Command Technical Documentation

The `secscan` command performs a full security-posture audit of an ARM Cortex-M SoC directly from a live GDB session, and can render a stored audit result as a console report or a standalone HTML report.

---

## Technical Overview

`secscan` runs a series of independent audit passes against the target's architected registers and produces a flat list of findings, each tagged with a category and a severity (`PASS`, `INFO`, `WARN`, `FAIL`):

1. **MPU** — presence, enable state, per-region validity, non-overlap, W^X enforcement, stack (MSP/PSP) executability, and generic RAM/Flash execute/write policy.
2. **CMSIS Core Security** — configurable fault handlers (`SHCSR`), `CCR` trap bits (`DIV_0_TRP`, `UNALIGN_TRP`, `BFHFNMIGN`), and debug-port state (`DHCSR`).
3. **TrustZone (SAU)** — Security Attribution Unit presence, activation, region count/overlap.
4. **Fault Handlers (VTOR)** — vector table base and presence/validity of critical exception handlers (NMI, HardFault, MemManage, BusFault, UsageFault, SVCall, PendSV, SysTick), cross-checked against `SHCSR` enable bits.
5. **ARMv8-M Stack Limits** — `MSPLIM`/`PSPLIM` configuration and consistency with the current `MSP`/`PSP` (ARMv8-M cores only).
6. **PACBTI (ARMv8.1-M)** — best-effort observation of PAC key material via GDB's optional `PAC_KEY_P_*` registers, and an informational note on BTI (Cortex-M52/M55/M85 only).
7. **STM32 Readout Protection (RDP)** — decoded from the vendor's Flash option register when the target is recognized as an STM32 part.

---

## Internal Audit Architecture

```
+-------------------------------------------------------------------------+
|                              gdb.selected_frame                         |
+-------------------------------------------------------------------------+
                              |
                              v
              +-------------------------------+
              | decode_cpuid + discover_rom   |
              | + DEFAULT_PROVIDER_REGISTRY    |
              +-------------------------------+
                              |
        +-----------+-----------+-----------+-----------+-----------+
        v           v           v           v           v           v
   _audit_mpu  _audit_cmsis _audit_    _audit_vtor  _audit_    _decode_
               _core        trustzone               stack_    stm32_rdp
                                                     limits /
                                                     _audit_
                                                     pacbti
        |           |           |           |           |           |
        +-----------+-----------+-----------+-----------+-----------+
                              |
                              v
              +-------------------------------+
              | _FindingCollector             |
              | (category, severity, detail)  |
              +-------------------------------+
                              |
                              v
              +-------------------------------+
              | SecscanReport                 |
              +-------------------------------+
                     |                 |
                     v                 v
           render_report()     dump_report_to_json() /
           (Rich console)      dump_report_to_html()
```

---

## Register Map

| Peripheral | Register | Address | Purpose |
| :--- | :--- | :--- | :--- |
| SCB | `VTOR` | `0xE000ED08` | Vector table base |
| SCB | `CCR` | `0xE000ED14` | Trap/behavior configuration bits |
| SCB | `SHCSR` | `0xE000ED24` | Fault handler enable/pending/active state |
| SCB | `CPACR` | `0xE000ED88` | Coprocessor/FPU access control |
| SCB | `NSACR` | `0xE000ED8C` | Non-secure access control (TrustZone) |
| MPU | `TYPE` | `0xE000ED90` | Number of implemented regions |
| MPU | `CTRL` | `0xE000ED94` | Enable, `HFNMIENA`, `PRIVDEFENA` |
| MPU | `RNR` | `0xE000ED98` | Region Number Register (selects the active region) |
| MPU | `RBAR` | `0xE000ED9C` | Region Base Address Register |
| MPU | `RASR`/`RLAR` | `0xE000EDA0` | Region Attribute/Limit register (v6/v7-M vs v8-M layout) |
| SAU | `CTRL` | `0xE000EDD0` | SAU enable, `ALLNS` |
| SAU | `TYPE` | `0xE000EDD4` | Number of implemented SAU regions |
| SAU | `RNR`/`RBAR`/`RLAR` | `0xE000EDD8`-`0xE000EDE0` | SAU region selection/base/limit |
| DCB | `DHCSR` | `0xE000EDF0` | Debug halting control and status |

Region iteration for both MPU and SAU requires selecting a region index via the `RNR` register before reading its `RBAR`/`RASR`/`RLAR` pair; `secscan` writes `RNR` transiently and restores its original value once the scan completes.

---

## MPU Region Decoding

Two hardware layouts are supported, selected from the decoded CPUID core name:

- **ARMv6-M / ARMv7-M** (`RBAR`/`RASR`): base address, `ENABLE`, `SIZE` (region size = `2^(SIZE+1)` bytes), `AP` (3-bit access permission), `XN`.
- **ARMv8-M** (`RBAR`/`RLAR`): base/limit addresses, `AP` (2-bit), `XN`, `EN`.

For each enabled region, `secscan` derives `writable` and `executable` booleans from the architecture-specific `AP`/`XN` encoding, then:

- Flags any region that is both `writable` and `executable` (W^X violation).
- Flags overlapping enabled regions.
- Flags regions with `limit <= base` (invalid size).
- Locates the region (if any) covering the current `MSP`/`PSP` and checks it is non-executable.
- Flags RAM regions (`0x20000000`-`0x40000000`) that are executable, and Flash/code regions (`0x00000000`-`0x20000000`) that are writable.

---

## PACBTI Best-Effort Check

Pointer Authentication and Branch Target Identification (Armv8.1-M, available on Cortex-M52/M55/M85) have no dedicated runtime enable register: BTI landing pads are always active for code compiled with the extension, and PAC strength depends only on key material and on the firmware being built with return-address signing. `secscan` therefore:

- Attempts to read the `PAC_KEY_P_0`..`PAC_KEY_P_3` registers through GDB.
- Reports `INFO` if unavailable (extension not implemented, or hidden by the target description).
- Reports `WARN` if the privileged key is all-zero.
- Reports `PASS` if key material is present.
- Always adds an `INFO` reminder that BTI protection depends on the `-mbranch-protection` compiler flags used to build the firmware.

---

## STM32 Readout Protection (RDP)

When the target is recognized as an STM32 part (via the shared `DEFAULT_PROVIDER_REGISTRY`), `secscan` looks up a documented Flash option register address/bit-shift for the product line family and decodes the standard RDP byte encoding:

| RDP byte | Level | Meaning |
| :--- | :--- | :--- |
| `0xAA` | 0 | No protection |
| `0xCC` | 2 | Fully protected, debug permanently disabled |
| other | 1 | Debug restricted / partial protection |

Families without a documented layout are reported as `INFO` rather than guessed.

---

## Command Reference

| Command | Description |
| :--- | :--- |
| `secscan audit [<output.json>]` | Run the full audit against the live target, print a Rich console report, and optionally dump the raw findings to a JSON file. |
| `secscan report <report.json>` | Load a previously dumped JSON audit and render it as a Rich console report. |
| `secscan report <report.json> --html <output.html>` | Load a previously dumped JSON audit and render it as a standalone, self-contained HTML file (inline CSS, no external assets, all report text HTML-escaped) instead of printing to the console. |
| `secscan help` | Show the command reference. |

### Examples

```
(gdb) secscan audit
(gdb) secscan audit /tmp/audit.json
(gdb) secscan report /tmp/audit.json
(gdb) secscan report /tmp/audit.json --html /tmp/audit.html
```

---

## Report Data Model

`SecscanReport` and `SecscanFinding` are plain, JSON-serializable dataclasses:

```json
{
  "core": "Cortex-M33",
  "vendor": "STMicroelectronics",
  "device_name": "STM32L552",
  "generated_at": "2026-09-15T12:00:00+00:00",
  "summary": {"FAIL": 0, "WARN": 1, "INFO": 3, "PASS": 8},
  "findings": [
    {"category": "MPU", "severity": "PASS", "title": "MPU is enabled", "detail": "8 region(s) implemented."}
  ]
}
```

This is the exact structure written by `secscan audit <file.json>` and read back by `secscan report <file.json>`, making audit results portable between machines and CI pipelines without requiring a live target connection.
