# Configuration examples

These files are starting points, not universal board configurations. Replace
executable paths, target names, probe serial numbers and board scripts with
values matching your installation. Initialization can reset the target and
load an SVD over the network.

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
launching pyGdbServer. The SWO port remains `2332`; change it when that port
is already in use. GDB and Telnet ports are allocated dynamically using
`{gdb_port}` and `{telnet_port}` instead of fixed ports `2331` and `2333`.

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

## Pico 2 W

Download the board configurations:

- {download}`OpenOCD <examples/boards/pico2w-openocd.json>`
- {download}`pyOCD <examples/boards/pico2w.json>`

The OpenOCD configuration contains installation-specific absolute paths and a
probe serial number. Adapt both; use an OpenOCD build with RP2350 support and
matching scripts. See [Pico 2 W access ports](dap.md#pico-2-w) for details.

The [server configuration reference](pygdbserver.md#configuration) explains each
field and the port placeholders.
