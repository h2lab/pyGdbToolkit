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
  "ocd-args": ["gdbserver", "--port", "{gdb_port}"],
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
| `ocd-args` | OCD arguments. `{gdb_port}` and `{loopback}` are expanded. pyOCD and OpenOCD receive suitable arguments automatically when placeholders are omitted. |
| `listen-address` | Public WebSocket address. Port `0` requests a dynamic API port. |
| `gdb-init` | GDB CLI commands run after connection and toolkit loading. |
| `log-directory` | Parent of timestamped run-log directories. |
| `startup-timeout` | Per-stage timeout in seconds. |

The OCD GDB endpoint and raw MI adapter always bind to `127.0.0.1` with dynamic
ports. The raw MI adapter is for local diagnostics; clients should use the API.
When exposing the public API beyond loopback, place it behind a trusted network
or a TLS and authentication reverse proxy.

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

### `mi.execute`

Runs a raw MI command, which must start with `-`.

```json
{"jsonrpc":"2.0","id":5,"method":"mi.execute","params":{"command":"-data-list-register-names"}}
```

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

### `svd.peripherals`

Returns the loaded SVD device and peripheral/register metadata as JSON. When no
description has been loaded, `loaded` is false and `peripherals` is empty.

```json
{"jsonrpc":"2.0","id":11,"method":"svd.peripherals"}
```

### `logs.get`

Returns events after sequence `since`; `limit` is from 1 to 10,000.

```json
{"jsonrpc":"2.0","id":7,"method":"logs.get","params":{"since":0,"limit":1000}}
```

Each event has `sequence`, UTC `timestamp`, `source`, `stream`, and `message`.
All events are retained in `events.jsonl` under the configured log directory.

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

Enter `help` in the dashboard prompt for the client commands, the available
pyGdbToolkit commands and subcommands, and links to the GDB and configured OCD
manuals. GDB CLI commands use `gdb <command>`; commands for the selected OCD use
`monitor <command>`.

`rtos load-project --from <path>` is executed by GDB on the **server**. The path
is therefore resolved in the server's filesystem, not the client's. For a
remote client, the project must be available to the server at that path, for
example through a shared mount. This is intentional: pyGdbToolkit keeps the
native GDB command usable unchanged in classic sessions that do not use
pyGdbServer, rather than adding client-specific path translation or implicit
project transfer.
