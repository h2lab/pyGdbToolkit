# pyGdbToolkit User Guide

pyGdbToolkit provides GDB commands for inspecting embedded targets, a supervised
debug server, and a terminal dashboard. This guide covers the commands and
configuration used in both direct GDB sessions and client-server sessions.

Start with the installation and first-session chapter, then use the command
reference for your inspection tasks. Technical details are grouped in the
appendices.

```{toctree}
:maxdepth: 2
:caption: Getting started

getting-started
configuration-examples
```

```{toctree}
:maxdepth: 2
:caption: Target inspection

lscpu
dap
memmap
svd
fault_info
secscan
rtos
```

```{toctree}
:maxdepth: 2
:caption: Server and client

pygdbserver
```

```{toctree}
:maxdepth: 2
:caption: Technical appendices

ocd
providers-svd
rtos-camelot
session
```
