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

## systemd multi-instance deployment

The examples in [`doc/examples`](examples) provide a systemd template and a
pyOCD configuration that binds each server process to the USB serial number
found through its TTY. Each instance gets its own OCD, GDB process, log
directory and WebSocket port. The server accepts one WebSocket client at a time;
connect that client to the port assigned to its instance.

For pyOCD/ST-LINK, the TTY (for example `/dev/ttyACM0`) identifies the probe,
but pyOCD communicates through its USB device node under `/dev/bus/usb`. For
that reason, the unit does not use `PrivateDevices=yes` or a TTY-only
`DeviceAllow=` rule: either would hide/block the USB node required by libusb.
Instead, the udev example makes ST-LINK V3 nodes inaccessible to the service
account by default and grants configured probes to the shared `pygdb` group.
The USB nodes remain visible in `/dev`; probes not matched by a serial-specific
rule cannot be opened by the unprivileged service account. Adapt the
vendor/product and serial matches for other probe types.

Install the package and create the shared service account, then install the
template, boot-time scanner, and a suitably edited udev rule:

```console
sudo useradd --system --user-group --create-home --home-dir /var/lib/pygdbserver --shell /usr/sbin/nologin pygdbserver
sudo install -d -o pygdbserver -g pygdbserver -m 0750 /var/lib/pygdbserver
sudo install -d -m 0750 /etc/pygdbserver
sudo install -D -m 0644 doc/examples/pygdbserver@.service /etc/systemd/system/pygdbserver@.service
sudo install -D -m 0644 doc/examples/pygdbserver.service /etc/systemd/system/pygdbserver.service
sudo install -D -m 0755 doc/examples/pygdbserver-start-all.py /usr/local/libexec/pygdbserver-start-all.py
sudo install -D -m 0644 doc/examples/pygdbserver-udev.rules /etc/udev/rules.d/99-pygdbserver.rules
```

Create the shared device-access group once and install the example instance
files. They are templates only; each probe must get its own pair of files:

```console
sudo groupadd --system pygdb
sudo install -m 0644 doc/examples/servercfg-instance.json /etc/pygdbserver/<instance>.json
sudo install -m 0644 doc/examples/pygdbserver-instance.conf /etc/pygdbserver/<instance>.conf
```

Each pair shares the same `<instance>` stem. In `<instance>.conf`, set
`PYGDBSERVER_CONFIG=/etc/pygdbserver/<instance>.json`, `DEVICE_PATH` to that
probe's `/dev/ttyUSBx` or `/dev/ttyACMx`, and a unique `LISTEN_ADDRESS` port.
Prefer a stable `/dev/serial/by-id/...` path when available. In the paired JSON,
configure the GDB binary and arguments, OCD binary and arguments, target, and
GDB initialization commands for that probe. The JSON file is not shared
between instances; `servercfg-instance.json` is a starting template whose
sample target (`stm32u5a5zjtxq`) must be changed to match each board. Add a
matching serial-specific udev rule for every probe, assigning each to the
shared `pygdb` group; start from
[`pygdbserver-udev.rules`](examples/pygdbserver-udev.rules). Set
`{usb_serial}` in pyOCD arguments to select the probe from the TTY. If
`pyGdbServer` is installed in a virtual environment, change the `ExecStart`
executable in the unit to its absolute path.

The scanner uses each `.conf` filename stem as the systemd instance name. For
example, `/etc/pygdbserver/stlink-lab.conf` paired with
`/etc/pygdbserver/stlink-lab.json` starts
`pygdbserver@stlink-lab.service`. In the template unit
`pygdbserver@.service`, systemd substitutes `%i` with `stlink-lab`, so
`EnvironmentFile=/etc/pygdbserver/%i.conf` loads the matching environment file;
the file's `PYGDBSERVER_CONFIG` then selects the matching JSON. The scanner
requires that `PYGDBSERVER_CONFIG` point to `/etc/pygdbserver/<instance>.json`
and skips a connected device if this paired JSON file is missing.

### Install pyOCD packs for the service account

pyOCD stores user-installed CMSIS-Packs in its per-user data directory. The
instance unit sets `HOME=/var/lib/pygdbserver`, so install and inspect packs as
that same account; installing them as root or as an interactive user will put
them in a different context and the service may not find them. The `pyocd`
executable used below must be the same installation that the service resolves
from `ocd-path` (and must be readable/executable outside any home directory
hidden by systemd sandboxing).

For the sample target:

```console
sudo -u pygdbserver -H pyocd pack install stm32u5a5zjtxq
sudo -u pygdbserver -H pyocd pack show
```

`-H` selects the service account's configured home, `/var/lib/pygdbserver`.
Run `pack install` once for each target family that needs a pack. `pack show`
should list the downloaded pack when run as `pygdbserver`; that is the same
user and home used by the systemd instances. If `pyocd` is not in the service
account's `PATH`, use its absolute executable path in both commands and in
`ocd-path` in each instance JSON file.

Reload udev after creating the shared group and installing all serial-specific
rules:

```console
sudo udevadm control --reload
```

Unplug and reconnect the probe so the rules are applied. The `.conf` filename
stem is used as the systemd instance name and must match the probe serial. Then
enable the scanner service; it waits for udev, checks each `DEVICE_PATH`, and
queues only instances whose path is a character device:

```console
sudo systemctl daemon-reload
sudo systemctl enable --now pygdbserver.service
```

No device present, or no `.conf` files, is a successful no-op: the corresponding
`pygdbserver@…` unit is not started. The scanner uses `systemctl start --no-block`
so all detected instances start independently. To rescan after plugging in a
probe later, run `sudo systemctl restart pygdbserver.service`. Repeat the udev
rule, environment file and port for each additional probe. All instances use
the same `pygdb` group, so Linux device permissions allow each instance access
to every probe matched by these rules. PyOCD still selects its configured probe
by USB serial, but the common group does not enforce per-process device
isolation. For example, clients can connect to
`ws://debug-host:1234` and `ws://debug-host:1235`. These API endpoints have no
built-in authentication or TLS; expose them only on a trusted network or behind
a protected reverse proxy. Each started instance restarts if the server exits.

This udev/group arrangement restricts which USB probes the service account can
open, but it does not hide the other USB device-node names from `/dev`. A
literal per-process device namespace for libusb would need a dynamic mount or
device-cgroup setup tied to the current USB bus/device numbers, which can
change whenever the probe reconnects; a static TTY-based systemd unit cannot
reliably provide that stronger visibility boundary.

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
Execution commands such as `continue` may return before later breakpoint output.
For example, `rtos showsched 8` arms a trace and `gdb continue` resumes the
target; when tracing completes, the Rich chart arrives asynchronously as a
`log.event` notification and is shown in the dashboard's command-output pane.

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

Enter `help` in the dashboard prompt for the client commands, the available
pyGdbToolkit commands and subcommands, and links to the GDB and configured OCD
manuals. GDB CLI commands use `gdb <command>`; commands for the selected OCD use
`monitor <command>`.

The command prompt retains the last 40 submitted commands. Use `Up` and `Down`
to browse them; enter `history` to list the retained commands in the output
panel. This history is local to the client session.

`rtos load-project --from <path>` is executed by GDB on the **server**. The path
is therefore resolved in the server's filesystem, not the client's. For a
remote client, the project must be available to the server at that path, for
example through a shared mount. This is intentional: pyGdbToolkit keeps the
native GDB command usable unchanged in classic sessions that do not use
pyGdbServer, rather than adding client-specific path translation or implicit
project transfer.
