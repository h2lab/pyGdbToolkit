<!--
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# Configuration examples

These files are starting points, not universal board configurations. Replace
executable paths, target names, probe serial numbers and board scripts with
values matching your installation. Initialization can reset the target and
load an SVD over the network.

All examples declare `ocd-identifier` separately from the executable path.
Backend values are `jlinkgdbserver`, `openocd`, and `pyocd`. Multicore context
models and the generic J-Link core mapping are described in [SMP support](smp.md).

## OpenOCD with STM32U5

Download {download}`servercfg-openocd.json <examples/servercfg-openocd.json>`.

```{literalinclude} examples/servercfg-openocd.json
:language: json
```

## pyOCD with STM32U5

Download {download}`servercfg-pyocd.json <examples/servercfg-pyocd.json>`.
Replace the `-u` probe identifier; use the target name supported by your pyOCD
installation.

```{literalinclude} examples/servercfg-pyocd.json
:language: json
```

(imx8mp-m7-jlink)=
## J-Link with i.MX8MP Cortex-M7

Download {download}`imx8mp-m7-jlink.json <examples/boards/imx8mp-m7-jlink.json>`.
This configuration uses `JLinkGDBServer`, the JTAG interface, and SEGGER's
`MIMX8ML6_M7` device identifier. The speed is 1000 kHz and the target is
little-endian. GDB's ARM architecture is set before connecting; pyGdbServer
automatically uses `target remote`, not `extended-remote`.

Stop any manually started J-Link GDB Server using the same probe before
launching pyGdbServer. GDB, Telnet and SWO ports are allocated dynamically using
`{gdb_port}`, `{telnet_port}` and `{swo_port}` instead of fixed ports
`2331`, `2333` and `2332`.

The private J-Link endpoint uses `-localhostonly 1`, not `-nolocalhostonly`.
Remote clients connect to pyGdbServer's WebSocket API, not directly to J-Link.
The API listens on `localhost:1234` in this example.

```console
pyGdbServer doc/examples/boards/imx8mp-m7-jlink.json
```

Initialization includes `monitor reset`, which resets and halts the Cortex-M7.
J-Link does not use OpenOCD's `monitor reset halt` syntax. Remove the reset
command when the existing target state must be preserved, and configure
J-Link's startup reset policy accordingly (for example `-noreset`).

```{literalinclude} examples/boards/imx8mp-m7-jlink.json
:language: json
```

(imx8mp-a53-jlink)=
## J-Link with i.MX8MP Cortex-A53

Download {download}`imx8mp-a53.json <examples/boards/imx8mp-a53.json>`.
This configuration uses `gdb-multiarch` and `JLinkGDBServer` with SEGGER's
`MIMX8ML6_A53_0` device identifier, JTAG at 1000 kHz, and little-endian access.
GDB's `aarch64` architecture and 60-second remote timeout are set before
connecting. pyGdbServer automatically uses `target remote`.

The initial `-device` matches ID `0` in the explicit `jlink-core-devices` mapping,
which declares all four
A53 devices and enables `dap core` pivots through one supervised J-Link server
per core. All four cores must already be accessible and powered; the configuration
does not start secondary cores. Startup normally halts the whole configured
cluster sequentially, not atomically. Remove the mapping for single-core operation.
See [SMP support](smp.md) for per-core halt/breakpoint behavior and SMP
limitations. `lscpu` also supports AArch64 CPU identification; other diagnostic
commands have not gained AArch64 implementations.
See [AArch64 lscpu](lscpu.md) for available register information
and J-Link limitations.

`-noreset` and `-noir` disable startup reset and register initialization, and
`gdb-init` contains no reset command. Connecting can still halt the selected
CPU. `set mem inaccessible-by-default off` changes GDB's memory-access policy;
it does not disable CPU caches.

Stop any manually started J-Link GDB Server using the same probe before
launching this example. GDB, Telnet and SWO ports use the dynamic `{gdb_port}`,
`{telnet_port}` and `{swo_port}` placeholders. In cluster mode, each core receives
its own distinct allocated ports; single-core operation also uses dynamic SWO.
The private endpoint
is localhost-only and the public WebSocket API listens on `localhost:1234`.
Change conflicting public API ports before running multiple instances.

```console
pyGdbServer doc/examples/boards/imx8mp-a53.json
```

```{literalinclude} examples/boards/imx8mp-a53.json
:language: json
```

(imx8mp-a53-openocd)=
## OpenOCD with i.MX8MP Cortex-A53 and J-Link

Download the {download}`server configuration <examples/boards/imx8mp-a53-openocd.json>`
and {download}`OpenOCD target script <examples/boards/imx8mp-a53-openocd.cfg>`.
The probe is still SEGGER J-Link, but the GDB server is **OpenOCD**, not
JLinkGDBServer. Stop other processes using the probe before launching this example.
Run from the repository root so the target-script path resolves correctly:

```console
pyGdbServer doc/examples/boards/imx8mp-a53-openocd.json
```

The tested executable is `/usr/bin/openocd` 0.12.0, with scripts under
`/usr/share/openocd/scripts`. Adapt these paths to your installation and confirm
that your build includes both the `jlink` adapter and the `aarch64` target.
On the validation host, the default PATH resolved to a ST fork without J-Link,
and `/usr/local/bin/openocd` also lacked that driver. A version banner alone
does not establish adapter support.

The target configuration follows the debug/CTI addresses declared in OpenOCD's
`target/imx8m.cfg` and `target/imx8mp.cfg`: APB-AP 1, debug bases
`0x80410000` through `0x80710000`, and corresponding CTI bases
`0x80420000` through `0x80720000`. Board-specific addresses stay in this file,
not the toolkit backend. It declares only the four A53 CPU targets, with
`-coreid`, `-rtos hwthread`, and one `target smp` group. No M7 or system-memory
target is examined. An additional `mem_ap` view of APB-AP 1 has its GDB port
disabled and supplies external CPU identification registers to `lscpu`.

`reset_config none` and startup `init; halt` attach without issuing a reset.
This still halts the running SMP cluster and can affect its OS and watchdogs.
The example does not power up secondary cores or change board clocks. It
requires all four A53 debug interfaces to be accessible. The debugger's own
connect, halt and disconnect actions are not guaranteed to preserve execution.

GDB and Telnet use `{gdb_port}` and `{telnet_port}`; TCL is disabled. There is
one GDB SMP endpoint, not one port per core, and no `jlink-core-devices` node.
`dap core list`, `dap core N`, normal GDB register/breakpoint commands and
`lscpu` use the same interface as the adjacent JLinkGDBServer example.
See [AArch64 SMP comparison](smp.md) for backend-specific semantics and limits.

```{literalinclude} examples/boards/imx8mp-a53-openocd.json
:language: json
```

```{literalinclude} examples/boards/imx8mp-a53-openocd.cfg
:language: text
```

## Pico 2 W

Download the board configurations:

- {download}`OpenOCD <examples/boards/pico2w-openocd.json>`
- {download}`pyOCD <examples/boards/pico2w.json>`

The OpenOCD configuration contains installation-specific absolute paths and a
probe serial number. Adapt both; use an OpenOCD build with RP2350 support and
matching scripts. See [Pico 2 W access ports](dap.md#pico-2-w) for details.

The [server configuration reference](pygdbserver.md#configuration) explains each
field and the port placeholders.
