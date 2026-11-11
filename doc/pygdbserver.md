<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# pyGdbServer

`pyGdbServer` is a standalone executable using pyGdbToolkit. It supervises the
OCD and GDB, connects GDB to the target, loads the toolkit, and exposes the
debug session to network clients.

## Start

```console
python -m pip install -e .
pyGdbServer stm32u5a5.json
```

Startup is ordered: OCD, OCD GDB-port readiness, GDB MI3 (MI2 fallback), target
connection, `python import pyGdbToolkit`, configured GDB initialization, and
finally the public API. A failed stage stops the complete stack.

## Configuration

```json
{
  "gdb-path": "gdb-multiarch",
  "gdb-args": [],
  "ocd-path": "pyocd",
  "ocd-identifier": "pyocd",
  "ocd-args": ["gdbserver", "--port", "{gdb_port}", "-T", "{telnet_port}"],
  "listen-address": "127.0.0.1:1234",
  "gdb-init": ["monitor reset halt"],
  "log-directory": ".pygdbserver-logs",
  "startup-timeout": 15
}
```

| Field | Meaning |
|---|---|
| `gdb-path` | GDB executable. |
| `gdb-args` | Extra GDB arguments. They must not establish the target connection. |
| `ocd-path` | pyOCD, OpenOCD, SEGGER JLinkGDBServer (including JLinkGDBServerCLExe), QEMU, or another GDB-server executable. |
| `ocd-identifier` | Backend protocol: `jlinkgdbserver`, `openocd`, `pyocd`, or `qemu`. Overrides executable-name inference, including for wrappers. Legacy files may omit it. |
| `ocd-args` | OCD arguments. `{gdb_port}`, `{telnet_port}`, and `{loopback}` are expanded. J-Link also supports `{swo_port}`. Known backends receive GDB port arguments automatically when omitted; QEMU requires `-gdb tcp:{loopback}:{gdb_port}` when supplied explicitly. Telnet and SWO arguments must be supplied explicitly outside cluster mode. |
| `listen-address` | Public WebSocket address. Port `0` requests a dynamic API port. |
| `gdb-init` | GDB CLI commands run after connection and toolkit loading. |
| `log-directory` | Parent of timestamped run-log directories. |
| `startup-timeout` | Per-stage timeout in seconds. |
| `jlink-core-devices` | Optional mapping of non-negative core IDs to unique SEGGER device selectors. Allowed only for `ocd-identifier: jlinkgdbserver` (or a legacy inferred J-Link backend). The initial `-device` must belong to the mapping. |
| `target` | Optional declared metadata reported by [`target.context`](#targetcontext): `mcu`, `revision` (non-empty strings), and `debug-transport` (`jtag` or `swd`). Other keys are rejected. |

The OCD GDB endpoint and raw MI adapter always bind to `127.0.0.1` with dynamic
ports. The raw MI adapter is for local diagnostics; clients should use the API.
When exposing the public API beyond loopback, place it behind a trusted network
or a TLS and authentication reverse proxy.

### QEMU: hardware-free targets and CI

Set `ocd-path` to `qemu-system-arm` and `ocd-identifier` to `qemu`.
QEMU occupies the OCD process slot: it emulates the board and provides the
GDB remote endpoint itself. No OpenOCD, pyOCD, proxy or physical debug probe is
needed. pyGdbServer runs on the host and supervises QEMU, then GDB, then the
WebSocket API using its normal startup/shutdown sequence.

The backend adds `-gdb tcp:127.0.0.1:<dynamic-port>` when `-gdb` is absent,
and adds `-S` when omitted so the CPU is stopped before GDB connects.
An explicit endpoint must be `-gdb tcp:{loopback}:{gdb_port}` (literal
`127.0.0.1` is also accepted). Fixed/public endpoints, duplicate endpoints,
`-s` and `-daemonize` are rejected. GDB uses `extended-remote`; startup readiness
checks the loopback socket. No Telnet port is passed to QEMU.

Use the [QEMU STM32 example](configuration-examples.md#qemu-with-stm32f405-no-probe).
Supply firmware built for the emulated board, with a valid vector table.
`-kernel` loads the target image; `symbol-file` in `gdb-init` loads its ELF
symbols into GDB. Relative image and symbol paths are resolved from the server's
working directory, not the configuration directory.

Monitor requests use QEMU's HMP dialect, not OpenOCD Tcl or pyOCD commands:

```gdb
monitor help
monitor info registers
monitor system_reset
```

For QEMU, `command.execute` (including `gdb monitor ...`) and
`mi.execute` console requests route monitor commands through the checked OCD
backend. This routing belongs to the QEMU OCD adapter, not the generic MI
transport; other OCD modes keep their commands unchanged.
The same check applies to `gdb-init`. An HMP `unknown command` response
becomes an explicit GDB error and JSON-RPC `-32000` error, for example:
`QEMU mode does not support monitor command 'reset halt'`.
Invalid monitor arguments also become errors rather than successful results.
Startup fails if a configured initialization command is rejected.
`system_reset` resets the emulated system; it is not an alias for
OpenOCD's `reset halt`. Use it while stopped when a stopped reset is required.
After a successful reset, the backend flushes GDB's register/frame cache and
invalidates cached toolkit inspection so subsequent reads reflect the reset.

The toolkit detects QEMU independently from HMP help and reports CPU contexts
from GDB threads in the currently attached emulated CPU cluster. It does not
automatically attach other clusters or claim a physical-core inventory.
DP/AP commands fail explicitly because QEMU does not expose a physical debug
port. Unsupported STM32 registers and CoreSight tables can prevent automatic
MCU/SVD identification; use `svd read <file.svd>` for an explicit SVD. Loading
an SVD does not implement missing peripherals.

This mode supports reproducible CI tests of the real GDB/MI/WebSocket stack,
memory access, breakpoints, execution and error handling without hardware:

```console
tox -e qemu
```

The dedicated environment runs only the new QEMU functional suite with real
QEMU and Python-enabled GDB. It generates a tiny bootable firmware without a
cross-compiler, uses dynamic ports and temporary logs, and terminates both
processes after each test. Install `qemu-system-arm` and `gdb-multiarch` on the
CI runner; GDB must embed Python 3.12+ with the toolkit's Python dependencies
available. The suite supplies the test environment's pure-Python dependencies
to GDB. A QEMU build must provide `netduinoplus2`, `stm32vldiscovery` and
`b-l475e-iot01a` (`qemu-system-arm -machine help`).
Use `PYGDB_QEMU` and `PYGDB_QEMU_GDB` to override executable names or paths.
Requested functional tests fail if tools are missing; ordinary test runs skip
them unless `PYGDB_QEMU_TESTS=1`. Existing tests remain unchanged.

This is not a substitute for hardware validation of Flash programming,
electrical debug access, STM32-specific security protections, or real-time
peripheral behavior. The listed STM32 models do not emulate STM32U5 or STM32N6.

### J-Link

For `JLinkGDBServer` or `JLinkGDBServerCLExe`, pyGdbServer connects using
`target remote`; other OCDs retain `target extended-remote`. J-Link does not
support extended-remote mode.

Set `ocd-path` to your SEGGER executable and supply your target-specific
`-device`, `-if`, probe selection, and speed arguments in `ocd-args`.
pyGdbServer adds `-localhostonly 1` and `-port` with its allocated GDB port.
An explicit `-port` (or `-p`) must use `{gdb_port}`; `-localhostonly 0` is rejected.
For multiple instances, configure `-telnetport {telnet_port}` explicitly and
use `-swoport {swo_port}`. In `jlinkgdbserver` mode, pyGdbServer allocates three
distinct loopback ports together for GDB, Telnet and SWO, including single-core
sessions. The SWO placeholder is not supported for other backend identifiers.
Explicit fixed SWO ports remain supported, but can conflict across instances.

```json
{
  "ocd-identifier": "jlinkgdbserver",
  "ocd-path": "JLinkGDBServer",
  "ocd-args": [
    "-device", "YOUR_DEVICE", "-if", "JTAG",
    "-port", "{gdb_port}",
    "-telnetport", "{telnet_port}",
    "-swoport", "{swo_port}"
  ]
}
```

This fragment only illustrates the port settings. Complete the target-specific
arguments and other fields using the [configuration examples](configuration-examples.md).

J-Link identification and ADIv5 JTAG-DPv0 AP inspection are supported.
Successful J-Link connection logs also provide the attached Cortex-M identity
to `dap core` and the core RPCs. The single local core `0` has scope
`attached-core-only`; selecting it does not change target state. An unknown
log format leaves identity unavailable. Discovery of other SoC cores,
switching to unconfigured cores and J-Link SWD/ADIv6 AP inspection are not implemented.
AP selection is local to toolkit profiling; it does not reroute GDB memory
accesses. See [ocd.md](ocd.md).

Configured J-Link multicore sessions require **pyGdbServer** to supervise
per-core servers and register their endpoints with GDB. This mechanism is
independent of device spelling and CPU model; actual identity evidence depends
on the architecture-specific reader. See [SMP support](smp.md) for context
models, generic configuration, supervision, and safety limitations.

An {ref}`i.MX8MP Cortex-M7 J-Link example <imx8mp-m7-jlink>`
provides the `MIMX8ML6_M7` JTAG configuration, startup commands and port settings.
An {ref}`i.MX8MP Cortex-A53 J-Link example <imx8mp-a53-jlink>`
provides the `MIMX8ML6_A53_0` configuration with AArch64 GDB setup and no startup
reset. Its `jlink-core-devices` mapping enables per-core cluster pivots using
`dap core` and the core RPCs. pyGdbServer supervises one J-Link process per
configured core and allocates distinct GDB, Telnet and SWO ports for each.
Additional GDB inferiors are attached lazily and available architectural
identity is checked after selection. Startup normally halts every configured core; shared-target
reset/load initialization is rejected and `-noreset -noir` is enforced.
Removing the mapping keeps the ordinary single-server configuration.
This is not an atomic all-core halt/resume interface; see [dap.md](dap.md) for
SMP limitations and [lscpu.md](lscpu.md) for AArch64 CPU-report support.

### OpenOCD AArch64 SMP

The {ref}`i.MX8MP Cortex-A53 OpenOCD example <imx8mp-a53-openocd>` uses the
same physical J-Link probe through OpenOCD's `jlink` adapter. Declare
`ocd-identifier: openocd`, select an executable built with `jlink` and `aarch64`
support, and supply the target Tcl file. Do not add `jlink-core-devices`;
OpenOCD owns the SMP group and exposes named hardware-core threads on one GDB
endpoint. pyGdbServer uses `extended-remote`, and no extra GDB inferiors or
per-core server processes are created.

The toolkit's OpenOCD backend synchronizes the selected GDB thread and monitor
target before subsequent CPU/AP operations. `dap core`, the core RPCs, GDB
register/breakpoint commands and AArch64 `lscpu` use the same public interface
as the JLinkGDBServer mode, but register availability and halt/resume behavior
remain backend-specific. See [SMP support](smp.md) for a side-by-side comparison.
The example starts with `init; halt`, not reset; connecting still halts the
running cluster. Its explicit APB debug view allows external MIDR reading without
creating a fifth GDB CPU context or examining the auxiliary M7.

### Multiple instances on the same loopback address

Use a different public `listen-address` port in each instance's JSON file.
pyGdbServer selects distinct free GDB and Telnet ports on `127.0.0.1` using the
operating system's port-`0` allocation and substitutes them for `{gdb_port}` and
`{telnet_port}` in `ocd-args`. J-Link additionally allocates `{swo_port}` in the
same operation. The sockets are held during allocation to ensure
the ports differ, then released before OCD starts. Another process could still
claim a port between allocation and OCD startup.

For **pyOCD**, keep `--port {gdb_port}` for GDB and add `-T {telnet_port}`
(equivalently, `--telnet-port {telnet_port}`) to use the allocated Telnet port instead of
the default `4444`. Each option and its value must be separate JSON strings:

```json
{
  "ocd-path": "pyocd",
  "ocd-args": ["gdbserver", "--port", "{gdb_port}", "-T", "{telnet_port}"]
}
```

pyGdbServer supplies `{telnet_port}` but does **not** add the Telnet option
automatically. Add it explicitly to each configuration used for concurrent
pyOCD instances. Existing configurations without this placeholder retain their
Telnet settings, including `-T 0` if allocation by pyOCD itself is preferred.

For **OpenOCD**, use `gdb_port {gdb_port}` for GDB and configure both auxiliary
listeners: Telnet defaults to `4444` and TCL to `6666`. Their equivalent dynamic
port settings are `-c "telnet_port {telnet_port}"` and `-c "tcl_port 0"`.
The Telnet port comes from pyGdbServer; TCL port allocation remains OpenOCD's
responsibility:

```json
{
  "ocd-path": "openocd",
  "ocd-args": [
    "-c", "gdb_port {gdb_port}",
    "-c", "telnet_port {telnet_port}",
    "-c", "tcl_port 0",
    "-f", "interface/stlink.cfg",
    "-f", "target/stm32f4x.cfg"
  ]
}
```

Replace the interface and target files with those for your hardware. If Telnet
and TCL are not needed, use `telnet_port disabled` and `tcl_port disabled`
instead. Alternatively, assign distinct fixed ports to each instance.
These settings must be applied before OpenOCD's `init`; configuration scripts
must not override them. pyGdbServer adds neither the Telnet nor the TCL settings.
Any additional listeners enabled by your OCD configuration also need distinct
ports or must be disabled.

See the [pyOCD GDB server documentation](https://pyocd.io/docs/gdbserver.html)
and [OpenOCD TCP/IP port configuration](https://openocd.org/doc/html/Server-Configuration.html#TCP_002fIP-Ports).

## Protocol

The API is **JSON-RPC 2.0 over RFC 6455 WebSocket**. One WebSocket text message
contains one request, response, or notification. UTF-8 JSON binary messages are
also accepted. The maximum message size is 8 MiB.

```json
{"jsonrpc":"2.0","id":1,"method":"command.execute","params":{"command":"lscpu"}}
```

```json
{"jsonrpc":"2.0","id":1,"result":{"class":"done","record":"done","output":["Cortex-M CPU report\n"]}}
```

A request without `id` is a notification and receives no response. Standard
codes `-32700`, `-32600`, `-32601`, and `-32602` describe protocol errors;
`-32000` describes process or command failures.

## Methods

### `command.execute`

Runs a pyGdbToolkit or GDB CLI command. Commands are serialized. Optional
`timeout` is limited to 300 seconds.

```json
{"jsonrpc":"2.0","id":2,"method":"command.execute","params":{"command":"fault_info"}}
{"jsonrpc":"2.0","id":3,"method":"command.execute","params":{"command":"gdb info registers"}}
{"jsonrpc":"2.0","id":4,"method":"command.execute","params":{"command":"monitor reset halt"}}
```

The `gdb ` prefix is removed. `monitor` is forwarded unchanged to the OCD.
Commands without a prefix are resolved by GDB, including toolkit commands.
QEMU monitor requests use the checked HMP backend described above; other
backends retain their existing forwarding behavior.
This includes every `memmap` operation: discovery, verification, baselines,
sampling, display, help and JSON report export. It uses the same
`command.execute` route, not a dedicated `memmap.*` execution API. For example:

```json
{"jsonrpc":"2.0","id":25,"method":"command.execute","params":{"command":"memmap discover --verify","timeout":60}}
```

`memmap` also appears in `toolkit.commands` and `toolkit.help`. See
[memory mapping](memmap.md) for the confidence model and sampling safeguards.
Execution commands such as `continue` may return before later breakpoint output.
For example, `rtos showsched 8` arms a trace and `gdb continue` resumes the
target; when tracing completes, the Rich chart arrives asynchronously as a
`log.event` notification and is shown in the dashboard's command-output pane.

### `mi.execute`

Runs a raw MI command, which must start with `-`.

```json
{"jsonrpc":"2.0","id":5,"method":"mi.execute","params":{"command":"-data-list-register-names"}}
```

### `workspace.upload`

Installs a file in the server workspace: the working directory captured when
the server is created. `filename` is a plain file name of at most 128
characters, without directory separators or components (`.` and `..` are
rejected); `content` is strict base64-encoded binary data, limited to 5 MiB
before encoding. A `directory` parameter is rejected: uploads cannot target
any other server directory. Existing files are replaced atomically. This
method is independent of GDB and OCD.

```json
{"jsonrpc":"2.0","id":12,"method":"workspace.upload","params":{"filename":"example.txt","content":"aGVsbG8K"}}
{"jsonrpc":"2.0","id":12,"result":{"path":"/workspace/example.txt","size":6}}
```

### `workspace.download`

Reads a file from the server workspace in bounded chunks. `filename` follows
the same rules as uploads: a plain file name of at most 128 characters,
without directory separators or components. A `directory` parameter is
rejected, and files resolving outside the workspace (for example through a
symbolic link) are refused. `offset` is a non-negative byte offset and
`chunk_size` is limited to 1 MiB; the result reports the total file `size`
and the base64-encoded chunk `content`.

```json
{"jsonrpc":"2.0","id":14,"method":"workspace.download","params":{"filename":"example.txt","offset":0,"chunk_size":1048576}}
{"jsonrpc":"2.0","id":14,"result":{"filename":"example.txt","offset":0,"size":6,"content":"aGVsbG8K"}}
```

### `workspace.list`

Lists the server workspace by default, or an existing `directory` resolved
from that workspace. Entries are sorted by name and identify directories.
This method does not send any GDB or OCD command.

```json
{"jsonrpc":"2.0","id":13,"method":"workspace.list","params":{"directory":"/tmp"}}
{"jsonrpc":"2.0","id":13,"result":{"path":"/tmp","entries":[{"name":"example.txt","is_directory":false}]}}
```

File transfers are confined to the server workspace, but these methods still
use the server process's filesystem permissions: expose the API only to
trusted clients.

### `server.status`

Returns process IDs, selected MI version, dynamic internal ports, API address,
readiness, and persistent log path.

```json
{"jsonrpc":"2.0","id":6,"method":"server.status"}
```

### `target.status`

Returns the selected GDB thread/core and its execution state (`running` or
`stopped`). The Access Port is included when pyOCD reports it in its startup
logs; otherwise it is `null`, not inferred.

```json
{"jsonrpc":"2.0","id":10,"method":"target.status"}
```

### `target.cores`, `target.core`, `target.select_core`

Discover physical CPU cores, report the active core, or select a core for the
shared GDB session. These methods use the same runtime as `dap core`:

```json
{"jsonrpc":"2.0","id":20,"method":"target.cores"}
{"jsonrpc":"2.0","id":21,"method":"target.core"}
{"jsonrpc":"2.0","id":22,"method":"target.select_core","params":{"core":1}}
```

`target.cores` returns `{"cores": [...]}`. The other two return `{"core": {...}}`:

```json
{"jsonrpc":"2.0","id":22,"result":{"core":{"id":1,"name":"rp2350.cm1","selected":true,"endpoint":"127.0.0.1:3333","inferior":1,"thread":2}}}
```

Each descriptor includes the physical `id`, server `name`, selection flag,
TCP `endpoint`, GDB `inferior` number and global GDB `thread` ID. Inferior/thread
fields are `null` when unavailable (in particular, a pyOCD core not yet attached).
The `core` parameter must be a non-negative JSON integer, not a boolean or string.
Invalid parameters return `-32602`; unavailable cores and failed connections
return `-32000`. The optional `timeout` is limited to 300 seconds.

With pyOCD, pyGdbServer starts on core 0's lowest port; core N uses
`gdb_port + N`. Additional sockets are attached lazily as separate GDB inferiors.
OpenOCD uses named hardware-core threads on its existing SMP connection.
Core selection does not explicitly reset or resume the target; a new GDB
attachment may halt a core according to the server configuration.

Selection applies to all clients sharing this server, including subsequent
register/memory commands and toolkit diagnostics. `target.status` reports the
selected physical core. Each selection RPC executes as one serialized GDB
operation, but separate requests from different clients can interleave.

The console equivalent remains available:

```json
{"jsonrpc":"2.0","id":23,"method":"command.execute","params":{"command":"dap core list"}}
{"jsonrpc":"2.0","id":24,"method":"command.execute","params":{"command":"dap core 1"}}
```

See [CPU core selection](dap.md#cpu-core-selection) for standalone GDB usage and
the opt-in RP2350 hardware tests. `dap select` still selects only an Access Port.

### `target.context`

Returns a versioned snapshot of the target identity, intended for an LLM agent
or any automated client. Collection never probes the target: it reuses what the
toolkit session already knows (architecture probe from `lscpu`, confirmed
`memmap discover` results, loaded SVD, selected core), host-side GDB state, and
the server configuration.

```json
{"jsonrpc":"2.0","id":30,"method":"target.context","params":{"timeout":10}}
```

```json
{"jsonrpc":"2.0","id":30,"result":{
  "schema_version":1,
  "context_id":"ctx-3f9a0c5be1d27a44",
  "mcu":{"value":"Raspberry Pi RP2350","source":"toolkit-discovery","unavailable_reason":null},
  "revision":{"value":null,"source":"architecture-probe","unavailable_reason":"target not identified yet; run 'lscpu'"},
  "architecture":{"value":"armv8-m.main","source":"gdb","unavailable_reason":null},
  "debug_transport":{"value":"SWD","source":"configuration","unavailable_reason":null},
  "ocd":{"value":"openocd (openocd)","source":"configuration","unavailable_reason":null},
  "core":{"value":"rp2350.cm0 (id 0)","source":"debug-server","unavailable_reason":null},
  "memory_map":{"regions":[{"start":536870912,"end":537395200,"kind":"ram","source":"gdb-server","name":"SRAM","protection":"rw"}],
                "source":"toolkit-discovery","unavailable_reason":null},
  "svd_file":{"value":"/srv/svd/RP2350.svd","source":"svd","unavailable_reason":null}}}
```

Every fact has a `source`; an unknown fact has a `null` value and an
`unavailable_reason`. Sources and precedence:

| Fact | Sources, by precedence |
|---|---|
| `mcu` | Hardware-confirmed `memmap discover` fingerprint, then `target.mcu`, then the loaded SVD device name. |
| `revision` | Cached architecture probe (`unknown` is not reported), then `target.revision`. |
| `architecture` | Cached architecture probe, then the GDB inferior architecture. |
| `debug_transport` | `target.debug-transport`, then an explicit J-Link `-if` or OpenOCD `transport select` argument. pyOCD transports are not inferred. |
| `ocd` | `ocd-identifier` (or inferred backend) and executable name. |
| `core` | Selected core from the debug server, then the probed core name. |
| `memory_map` | Regions of the confirmed `memmap discover` report; unavailable otherwise. Regions are half-open and are not permission to access them. |
| `svd_file` | Path of the loaded SVD, on the **server** filesystem. |

`context_id` is derived from the content: it changes whenever any fact or the
memory map changes and is stable otherwise. When GDB cannot run the collector,
the configured facts are still returned and each GDB fact explains the failure.
The optional `timeout` is limited to 300 seconds.

### `svd.peripherals`

Returns the loaded SVD device and peripheral/register metadata as JSON. When no
description has been loaded, `loaded` is false and `peripherals` is empty.

```json
{"jsonrpc":"2.0","id":11,"method":"svd.peripherals"}
```

### `toolkit.commands`

Lists the pyGdbToolkit commands loaded in GDB, each with its `name` and
`summary`. Every command registers its own help in the toolkit session.

```json
{"jsonrpc":"2.0","id":12,"method":"toolkit.commands"}
```

### `toolkit.help`

Returns the help of every toolkit command, or only of `command` when given
(unknown names return `-32602`). Each entry has `name`, `summary`, `usage`
(a list of `syntax`/`description`), and `notes`. The client `help` command
renders this result.

```json
{"jsonrpc":"2.0","id":13,"method":"toolkit.help","params":{"command":"svd"}}
```

### `logs.get`

Returns events after sequence `since`; `limit` is from 1 to 10,000.

```json
{"jsonrpc":"2.0","id":7,"method":"logs.get","params":{"since":0,"limit":1000}}
```

Each event has `sequence`, UTC `timestamp`, `source`, `stream`, and `message`.
All events are retained in `events.jsonl` under the configured log directory.
Console text emitted later by GDB, outside the original command response, is
also delivered with `source: "gdb"` and `stream: "console"`.

### `logs.subscribe`

Subscribes this connection to subsequent log events.

```json
{"jsonrpc":"2.0","id":8,"method":"logs.subscribe"}
```

The server then emits notifications:

```json
{"jsonrpc":"2.0","method":"log.event","params":{"sequence":42,"timestamp":"2026-09-30T12:00:00+00:00","source":"gdb","stream":"mi","message":"(gdb)"}}
```

### `server.shutdown`

Requests orderly shutdown of WebSocket, GDB, and the OCD.

```json
{"jsonrpc":"2.0","id":9,"method":"server.shutdown"}
```

## Dashboard client

Install the project in the client environment and start the terminal dashboard:

```console
pyGdbClient
pyGdbClient ws://192.0.2.15:1234
```

The screen shows live OCD/GDB logs, command output, target execution state and
an expandable SVD peripheral/register tree. Enter toolkit or GDB commands
directly; use `gdb <command>` for explicit GDB commands and `monitor <command>`
for OCD commands. Opening a peripheral node runs `svd show <peripheral>`;
selecting a register runs `svd show <peripheral> <register>`. The client loads
the target-matched SVD at connect time and also supports explicit `svd read`
commands. Enter `quit` or press `Ctrl+Q` to exit only the dashboard. Enter
`quit --all` to request orderly shutdown of the server, GDB, and OCD before
the client exits.

A second prompt, below the command prompt, sends natural-language requests to
an optional LLM agent. Start the client with `--llm <provider>` to enable it;
see [Dashboard agent CLI](llm.md#dashboard-agent-cli).

Memory inspection uses the same command prompt and output panel as other
toolkit commands, with no separate command parser or automatic memory scan:

```text
memmap discover --verify
memmap show
memmap bases st
memmap probe --known --max-reads 16
memmap report "reports/mapping.json"
```

SVD loading is not required. `help` includes the registered `memmap` reference.
Report files are written by GDB on the server, and the destination directory
must already exist. Explicit-range sampling requires the same consent flags
as direct GDB; neither the client nor the server relaxes these rules.

The left panel shows the active physical CPU core and its TCP endpoint above
the logs. Its core selector lists the discovered CPUs and selects through
`target.select_core`; it is disabled when inventory is unavailable. The display
refreshes periodically and after a `dap core` command. A failed selection restores
the confirmed active core and reports the error in the command-output panel.
`dap core list`, `dap core`, and `dap core <id>` are also accepted directly at
the command prompt for both pyOCD and OpenOCD.

Enter `help` in the dashboard prompt for the client commands, the available
pyGdbToolkit commands and subcommands, and links to the GDB and configured OCD
manuals. GDB CLI commands use `gdb <command>`; commands for the selected OCD use
`monitor <command>`.

The command prompt retains the last 40 submitted commands. Use `Up` and `Down`
to browse them; enter `history` to list the retained commands in the output
panel. This history is local to the client session.

Enter `upload <file>` to copy a local file into the server workspace,
preserving its basename. The local path is unrestricted, but the resulting
server-side name must be a plain file name of at most 128 characters without
directory separators; uploads cannot target any other server directory.
Uploads support binary files up to 5 MiB and replace existing files. Enter
`download <server-file> [local-dir]` to copy a workspace file to a local
directory; the server-side name follows the same rules. Enter `ls [dir]` to
display the workspace or the specified server directory; directory names are
shown with a trailing `/`. Quote paths containing spaces. None of these
commands interacts with GDB or OCD; the dashboard's existing periodic
target-status polling remains unchanged.

```console
upload firmware.elf
upload "local files/device.svd"
download memory.bin ./dumps
ls
ls /tmp
```

`rtos load-project --from <path>` is executed by GDB on the **server**. The path
is therefore resolved in the server's filesystem, not the client's. For a
remote client, the project must be available to the server at that path, for
example through a shared mount. This is intentional: pyGdbToolkit keeps the
native GDB command usable unchanged in classic sessions that do not use
pyGdbServer, rather than adding client-specific path translation or implicit
project transfer.
