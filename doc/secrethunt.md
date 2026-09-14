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

Candidate memory blocks are classified by size, byte variance, structural heuristics, and entropy level:

- **Mathematical AES Key Schedule Verification**: Validates 176-byte (AES-128) and 240-byte (AES-256) blocks against the exact AES key expansion recurrence ($w_i = w_{i-4} \oplus \text{SubWord}(\text{RotWord}(w_{i-1})) \oplus \text{Rcon}_i$) to differentiate verified AES schedules from random high-entropy memory.
- **SHA Constant / Initial Vector Detection**: Checks for standard $H_0$ initial vectors (`0x6A09E667...`) and $K$ round constants (`0x428A2F98...`) to isolate SHA-256 / SHA-1 state contexts.
- **Thumb-2 Instruction Filtering in Flash**: When analyzing Flash memory on ARM Cortex-M targets, uses `THUMB_OPCODE_DICT` (a comprehensive 16-bit/32-bit opcode pattern dictionary) to detect Thumb-2 instruction sequences and exclude code blocks from secret analysis.
- **Digest / Key Format Matching**:

| Size | Entropy / Pattern | Classification Label | Likelihood |
| :--- | :--- | :--- | :--- |
| **Any** | $H_0$ Constants | SHA-256 Initial State / H Vector | High |
| **Any** | $K_i$ Constants | SHA-256 Round Constants (K Table) | High |
| **176 bytes** | AES Recurrence Match | Verified AES-128 Expanded Key Schedule | High |
| **240 bytes** | AES Recurrence Match | Verified AES-256 Expanded Key Schedule | High |
| **16 bytes** | $\ge 7.0$ | AES-128 Key / MD5 Digest (128 bits) | High |
| **20 bytes** | $\ge 7.0$ | SHA-1 / HMAC-SHA1 Digest Candidate (160 bits) | High |
| **28 bytes** | $\ge 7.0$ | SHA-224 Digest Candidate (224 bits) | High |
| **32 bytes** | $\ge 7.0$ | AES-256 Key / SHA-256 Digest / ECC-P256 Candidate | High |
| **48 bytes** | $\ge 7.0$ | SHA-384 Digest / ECC-P384 Key Candidate | High |
| **64 bytes** | $\ge 7.0$ | SHA-512 Digest / HMAC Block Candidate | High |
| **128, 256, 512 bytes** | $\ge 7.0$ | RSA Key Material ($N$ bits) | Medium |
| **Any** | $\ge 7.0$ | High-Entropy Secret / Token | Medium |
| **16 / 32 / 176 / 240 bytes** | $\ge 6.0$ | Medium-Entropy Candidate | Medium / Low |
| **Thumb-2 Code (Flash)** | Opcode Match | Thumb-2 Instruction Block | None |
| **Printable ASCII + `\0`** | Any | ASCII String Literal | None |
| **Uniform bytes** | Any | Uniform Pattern | None |

---

## Memory Region Configuration

Region base addresses and sizes must be configured explicitly before scanning using `secrethunt set`.

| Region Keyword | Description | Configuration Example |
| :--- | :--- | :--- |
| `sram` | SRAM memory range | `secrethunt set sram 0x20000000 0x20000` |
| `flash` | Flash memory range | `secrethunt set flash 0x08000000 0x40000` |
| `backup` | RTC / Backup register range | `secrethunt set backup 0x40002800 0x400` |

---

## GDB Usage and Subcommands

### `secrethunt set`
Configure the memory address range for a region keyword (`sram`, `flash`, `backup`).

```text
(gdb) secrethunt set sram 0x20000000 0x20000
(gdb) secrethunt set flash 0x18000000 0x20000
(gdb) secrethunt set backup 0x40002800 0x400
```

### `secrethunt scan`
Scan configured memory region(s) or custom address ranges for high-entropy byte blocks.

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
