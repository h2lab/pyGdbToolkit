# `fault_info` Command Technical Documentation

The `fault_info` command performs automated hardware fault diagnosis and stacked exception frame recovery on ARM Cortex-M microcontrollers.

---

## Technical Overview

When an ARM Cortex-M target hits a fault exception (HardFault, MemManage, BusFault, UsageFault, or SecureFault), `fault_info`:
1. Reads current core CPU registers and System Control Block (SCB) registers.
2. Identifies active fault bitflags and faulting memory addresses (`BFAR`, `MMFAR`, `SFAR`).
3. Decodes `EXC_RETURN` from `LR` to reconstruct the hardware-stacked exception frame from target RAM (`MSP` or `PSP`).
4. Performs address symbolication and memory region categorization.
5. Displays a diagnostic report with `Rich`.

---

## Internal Fault Analysis Architecture

```
+-------------------------------------------------------------------------+
|                              gdb.selected_frame                         |
+-------------------------------------------------------------------------+
       |                                              |
       v                                              v
+-----------------------+                      +--------------------------+
| Read Core Registers   |                      | Read SCB Registers       |
| (PC, LR, SP, xPSR)    |                      | (CFSR, HFSR, DFSR, etc.) |
+-----------------------+                      +--------------------------+
       |                                              |
       v                                              v
+-----------------------+                      +--------------------------+
| Decode EXC_RETURN     |                      | Bitflag Decoding         |
| (Stack: MSP/PSP, FPU) |                      | (UFSR, BFSR, MMFSR...)   |
+-----------------------+                      +--------------------------+
       |                                              |
       +----------------------+-----------------------+
                              |
                              v
              +-------------------------------+
              | Stack Frame Recovery          |
              | (R0-R3, R12, LR, PC, xPSR)    |
              +-------------------------------+
                              |
                              v
              +-------------------------------+
              | Symbolication & Memory Map    |
              | (_symbolicate, region lookup) |
              +-------------------------------+
                              |
                              v
              +-------------------------------+
              | Rich UI Rendering & Hints     |
              +-------------------------------+
```

---

## Detailed SCB Register Decoding

The command queries the following SCB register addresses:

| Register | Address | Description |
| :--- | :--- | :--- |
| **SCB_CFSR** | `0xE000ED28` | Configurable Fault Status Register (MMFSR + BFSR + UFSR) |
| **SCB_HFSR** | `0xE000ED2C` | HardFault Status Register |
| **SCB_DFSR** | `0xE000ED30` | Debug Fault Status Register |
| **SCB_MMFAR** | `0xE000ED34` | MemManage Fault Address Register |
| **SCB_BFAR** | `0xE000ED38` | BusFault Address Register |
| **SCB_SHCSR** | `0xE000ED24` | System Handler Control and State Register |
| **SCB_SFSR** | `0xE000EDE8` | Secure Fault Status Register (ARMv8-M) |
| **SCB_SFAR** | `0xE000EDEC` | Secure Fault Address Register (ARMv8-M) |

### Decoded Fault Types
- **UsageFault (UFSR)**: `UNDEFINSTR`, `INVSTATE`, `INVPC`, `NOCP`, `STKOF`, `UNALIGNED`, `DIVBYZERO`.
- **BusFault (BFSR)**: `IBUSERR`, `PRECISERR`, `IMPRECISERR`, `UNSTKERR`, `STKERR`, `LSPERR` + valid address in `BFAR`.
- **MemManage (MMFSR)**: `IACCVIOL`, `DACCVIOL`, `MUNSTKERR`, `MSTKERR`, `MLSPERR` + valid address in `MMFAR`.
- **HardFault (HFSR)**: Forced escalation (`FORCED`), vector table read error (`VECTTBL`).
- **SecureFault (SFSR)**: Security violations on ARMv8-M/TrustZone (`INVEP`, `INVIS`, `AUVIOL`, `INVTRAN`, etc.) + valid address in `SFAR`.

---

## Hardware Exception Unstacking Mechanism

When an exception occurs, Cortex-M hardware automatically pushes standard registers onto the active stack (`MSP` or `PSP`).

1. **`EXC_RETURN` Pattern Parsing**: `fault_info` inspects `LR`:
   - Bit `2`: `0` -> Stacked to Main Stack Pointer (`MSP`), `1` -> Process Stack Pointer (`PSP`).
   - Bit `4`: `0` -> Extended FPU frame stacked (32 additional words), `1` -> Standard 8-word frame.
   - Bit `0` (ARMv8-M): Target security domain (`0` -> Secure, `1` -> Non-Secure).
2. **Memory Frame Read**:
   - Standard frame reads 8 32-bit words: `R0`, `R1`, `R2`, `R3`, `R12`, `LR`, `PC`, `xPSR`.
   - FPU frame additionally reads `S0`-`S15` and `FPSCR`.
3. **Symbolication**:
   - Resolves stacked `PC` and `LR` addresses to function symbols using `gdb.execute("info symbol ...")`.

---

## Diagnostic Engine

`fault_info` generates tailored diagnostic recommendations:
- **Null-pointer dereference**: Triggered when `BFAR` or `MMFAR` points near `0x00000000`.
- **Stack overflow**: Triggered by `STKOF` bit in `UFSR` or stack frame pointer out of RAM bounds.
- **Divide by zero / Unaligned access**: Triggered by corresponding `UFSR` bits.
- **Execution of non-code**: Triggered when stacked `PC` points to non-executable memory or `INVSTATE` indicates T-bit loss.

---

## GDB Usage

```text
(gdb) fault_info
```

### Options
- Accepts no arguments. Fails with `gdb.GdbError` if invoked on non-ARM architectures.
