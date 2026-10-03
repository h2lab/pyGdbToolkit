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

## Pico 2 W

Download the board configurations:

- {download}`OpenOCD <examples/boards/pico2w-openocd.json>`
- {download}`pyOCD <examples/boards/pico2w.json>`

The OpenOCD configuration contains installation-specific absolute paths and a
probe serial number. Adapt both; use an OpenOCD build with RP2350 support and
matching scripts. See [Pico 2 W access ports](dap.md#pico-2-w) for details.

The [server configuration reference](pygdbserver.md#configuration) explains each
field and the port placeholders.
