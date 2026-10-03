<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# pyGdbToolkit

<img src="doc/logo.png" alt="pyGdbToolkit logo" width="200">

GDB commands for inspecting and diagnosing embedded targets, especially Arm
Cortex-M microcontrollers. The toolkit provides processor identification, debug
access-port inspection, fault analysis, SVD peripheral access, profiling,
security auditing and Camelot RTOS inspection.

Use it directly in GDB, or run `pyGdbServer` and `pyGdbClient` for a supervised
debug session with a terminal dashboard and a JSON-RPC automation interface.

## Installation

Requires Python 3.12+, GDB with compatible embedded Python, and a debug probe
with pyOCD or OpenOCD configured for your target. From a checkout:

```console
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

Follow the [first-session guide](doc/getting-started.md) for GDB setup or the
server/client workflow. Adapt an [example configuration](doc/configuration-examples.md)
to your board and probe. Keep the server API on loopback unless protected by
appropriate network, TLS and authentication controls.

## Build the documentation with tox

The [user guide](doc/index.md) is generated from the Markdown sources in `doc/`.
Tox creates an isolated environment and installs the Python documentation
dependencies from the `dev` extra. From the repository root:

```console
python -m pip install tox
npm install --global @mermaid-js/mermaid-cli@11
tox -e docs
```

Node.js and the Mermaid CLI (`mmdc` on `PATH`, with its Puppeteer browser) are
required to render diagrams. The HTML includes local resources and works
offline; no web server is needed. Open `doc/_build/html/index.html` after the
build. The continuous, single-page guide is in `doc/_build/singlehtml/index.html`;
keep its accompanying resource directories.

To generate the PDF as well, install GNU Make, `latexmk`, pdfLaTeX and the TeX
packages required by Sphinx. On Debian/Ubuntu:

```console
sudo apt install make latexmk texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended
tox -e docs,docs-pdf
```

The PDF is written to `doc/_build/latex/pyGdbToolkit-user-guide.pdf`. Both tox
environments fail on documentation warnings and unresolved internal references.
These tools are build-only dependencies, not application runtime requirements.

See [documentation build details](doc/README.md) for browser configuration,
distribution archives and source-maintenance instructions. The guide contains
the command reference, configuration examples, troubleshooting and technical
appendices.
