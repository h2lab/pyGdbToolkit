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
| `ocd-path` | pyOCD, OpenOCD, or another GDB-server executable. |
| `ocd-args` | OCD arguments. `{gdb_port}`, `{telnet_port}`, and `{loopback}` are expanded. pyOCD and OpenOCD receive GDB port arguments automatically when `{gdb_port}` is omitted; Telnet arguments must be supplied explicitly. |
| `listen-address` | Public WebSocket address. Port `0` requests a dynamic API port. |
| `gdb-init` | GDB CLI commands run after connection and toolkit loading. |
| `log-directory` | Parent of timestamped run-log directories. |
| `startup-timeout` | Per-stage timeout in seconds. |

The OCD GDB endpoint and raw MI adapter always bind to `127.0.0.1` with dynamic
ports. The raw MI adapter is for local diagnostics; clients should use the API.
When exposing the public API beyond loopback, place it behind a trusted network
or a TLS and authentication reverse proxy.

### Multiple instances on the same loopback address

Use a different public `listen-address` port in each instance's JSON file.
pyGdbServer selects distinct free GDB and Telnet ports on `127.0.0.1` using the
operating system's port-`0` allocation and substitutes them for `{gdb_port}` and
`{telnet_port}` in `ocd-args`. Both sockets are held during allocation to ensure
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

Installs a file in an existing server directory, independently of GDB and OCD.
`filename` is a basename without directory components; `content` is strict
base64-encoded binary data, limited to 5 MiB before encoding. Optional
`directory` defaults to the server workspace: the working directory captured
when the server is created. Relative directories are resolved from that
workspace; absolute paths such as `/tmp` are also accepted. Existing files
are replaced atomically. The destination directory must already exist.

```json
{"jsonrpc":"2.0","id":12,"method":"workspace.upload","params":{"filename":"example.txt","content":"aGVsbG8K","directory":"/tmp"}}
{"jsonrpc":"2.0","id":12,"result":{"path":"/tmp/example.txt","size":6}}
```

### `workspace.list`

Lists the server workspace by default, or an existing `directory` using the
same path rules as uploads. Entries are sorted by name and identify directories.
This method does not send any GDB or OCD command.

```json
{"jsonrpc":"2.0","id":13,"method":"workspace.list","params":{"directory":"/tmp"}}
{"jsonrpc":"2.0","id":13,"result":{"path":"/tmp","entries":[{"name":"example.txt","is_directory":false}]}}
```

These methods use the server process's filesystem permissions. They are not
restricted to the workspace, so expose the API only to trusted clients.

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

Enter `upload <file> [dir]` to copy a local file to the server, preserving its
basename. Without `dir`, it is installed in the server workspace; with `dir`,
it is installed in that server directory. Uploads support binary files up to
5 MiB and replace existing files. Enter `ls [dir]` to display the workspace or
the specified server directory; directory names are shown with a trailing `/`.
Quote paths containing spaces. Neither command interacts with GDB or OCD;
the dashboard's existing periodic target-status polling remains unchanged.

```console
upload firmware.elf
upload "local files/device.svd" /tmp
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
