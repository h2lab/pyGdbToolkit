# `secrethunt` Command Technical Documentation

The `secrethunt` command is a targeted entropy scanner and cryptographic material classifier for ARM Cortex-M microcontrollers.

---

## Technical Overview

`secrethunt` scans target memory spaces (SRAM, Flash, Backup Registers, or custom memory ranges) for regions exhibiting high Shannon entropy. High entropy is a primary characteristic of unencrypted cryptographic keys, expanded key schedules, seeds, and random tokens.

`secrethunt` provides three subcommands:
1. `scan`: Scans memory regions using a sliding analysis window.
2. `classify`: Applies heuristic size and pattern rules to identify cryptographic key formats.
3. `dump`: Exports detected candidate secrets to a structured JSON file.

---

## Technical Architecture & Internal Workflow

```
+-----------------------------------------------------------------------------+
|                          secrethunt Command Pipeline                        |
+-----------------------------------------------------------------------------+

 [Subcommand Parser (argparse)]
               |
               +-----------------------+-----------------------+
               |                       |                       |
               v                       v                       v
      [secrethunt scan]       [secrethunt classify]     [secrethunt dump]
               |                       |                       |
               v                       v                       v
     Chunked Read (4 KiB)    Apply Crypto Filter     Serialize Session
   TargetMemoryReader          "Key" / "AES" / High   Findings to JSON
               |                       |                       |
               v                       v                       v
     Sliding Window (32 B)       Rich Table Render       Panel Output File
    Shannon Entropy Calc
               |
               v
    Merge Ranges (Contiguous)
               |
               v
     Classify Candidates
               |
               v
    Update ScanSession
```

---

## Core Algorithms

### 1. Normalized Shannon Entropy Calculation (`calculate_entropy`)

Entropy is calculated per byte window:

$$H(X) = -\sum_{i=1}^{n} P(x_i) \log_2 P(x_i)$$

Where $P(x_i)$ is the empirical probability of byte value $x_i$ within the window. The result is normalized to a scale of $0.0$ to $8.0$ bits per byte.

### 2. Window Scanning & Range Merging (`scan_memory_region` & `merge_ranges`)

- Memory is read in **4 KiB chunks** via `TargetMemoryReader` to minimize GDB IPC overhead and handle memory read boundaries gracefully.
- A **32-byte sliding window** advances with a **16-byte step size**.
- Windows exceeding the entropy threshold (default: `6.0` bits/byte) record raw memory address bounds `(start_address, end_address)`.
- `merge_ranges` sorts raw address ranges and merges overlapping or adjacent regions into contiguous candidate blocks.

### 3. Classification Engine (`classify_finding`)

Candidate memory blocks are classified by size, byte variance, and entropy level:

| Size | Entropy | Classification Label | Likelihood |
| :--- | :--- | :--- | :--- |
| **16 bytes** | $\ge 7.0$ | AES-128 Key (Raw) | High |
| **32 bytes** | $\ge 7.0$ | AES-256 / ECC-P256 Key Candidate | High |
| **24, 48, 66 bytes** | $\ge 7.0$ | ECC Key Candidate ($N$ bits) | High |
| **176 bytes** | $\ge 7.0$ | AES-128 Expanded Key Schedule | High |
| **240 bytes** | $\ge 7.0$ | AES-256 Expanded Key Schedule | High |
| **128, 256, 512 bytes** | $\ge 7.0$ | RSA Key Material ($N$ bits) | Medium |
| **Any** | $\ge 7.0$ | High-Entropy Secret / Token | Medium |
| **16 / 32 / 176 / 240 bytes** | $\ge 6.0$ | Medium-Entropy Candidate | Medium / Low |
| **Uniform bytes** | Any | Uniform Pattern | None |

---

## Pre-defined Memory Map for ARM Cortex-M

| Region Name | Base Address | Default Size | Description |
| :--- | :--- | :--- | :--- |
| `sram` | `0x20000000` | 128 KiB | Primary internal SRAM |
| `flash` | `0x08000000` | 256 KiB | Internal Flash memory |
| `backup` | `0x40002800` | 1 KiB | RTC / Battery backup registers |
| `all` | All above | Scans SRAM, Flash, and Backup sequentially |

---

## GDB Usage and Subcommands

### `secrethunt scan`
Scan memory regions for high-entropy byte blocks.

```text
(gdb) secrethunt scan sram
(gdb) secrethunt scan flash --threshold 7.0
(gdb) secrethunt scan 0x20000000 0x10000 --window 64
(gdb) secrethunt scan all
```

### `secrethunt classify`
Display classified candidate secrets from the active scan session.

```text
(gdb) secrethunt classify
(gdb) secrethunt classify --crypto
```

### `secrethunt dump`
Export active scan findings to a JSON report.

```text
(gdb) secrethunt dump findings.json
```

### `secrethunt help`
Display usage instructions and command options.

```text
(gdb) secrethunt help
(gdb) secrethunt --help
(gdb) secrethunt -h
```
