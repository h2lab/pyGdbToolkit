# Building the user guide

The Markdown files in this directory are the shared source for HTML and PDF.
`index.md` defines the reading order. Sphinx uses MyST Parser, the Furo HTML
theme and sphinxcontrib-mermaid; no toolkit module is imported by the build.
The guide stays in English, matching the existing chapters.

## Install the build tools

From the repository root, in a Python 3.12+ virtual environment:

```console
python -m pip install -e '.[dev]'
```

Mermaid diagrams are rendered at build time, so the HTML can be used offline.
Install Node.js and Mermaid CLI separately:

```console
npm install --global @mermaid-js/mermaid-cli@11
```

The `mmdc` command must be on `PATH`, with its Puppeteer browser available.
When using a system Chromium rather than the downloaded browser, set
`PUPPETEER_EXECUTABLE_PATH` to its executable path. Browser system-library
requirements depend on your platform. Do not disable the browser sandbox on a
normal workstation.

PDF builds additionally require GNU Make, `latexmk`, pdfLaTeX and the TeX packages
used by Sphinx. On Debian/Ubuntu, a typical installation is:

```console
sudo apt install make latexmk texlive-latex-recommended texlive-latex-extra texlive-fonts-recommended
```

These are build-only tools, not application runtime dependencies.

## Build

From the repository root with the environment activated:

```console
make -C doc html
make -C doc singlehtml
make -C doc pdf
make -C doc dist
```

Set `PYTHON=/absolute/path/to/python` when the desired interpreter is not
`python3`. Outputs are:

| Target | Output |
| --- | --- |
| `html` | `_build/html/index.html`, navigation, search, images and downloads |
| `singlehtml` | `_build/singlehtml/index.html`, a continuous guide with local assets |
| `pdf` | `_build/latex/pyGdbToolkit-user-guide.pdf` |
| `dist` | Both HTML ZIP archives and the PDF directly under `_build/` |

HTML archives include local assets and downloadable example configurations.
Extract the complete archive before opening `index.html`; the monopage output
is not a single resource-embedded HTML file. No web server is required.
The PDF contains the generic configuration listings, but downloadable board
files are available in the HTML distribution only.

Use `make -C doc clean` to delete only generated documentation output. Clean
before a release build to avoid retaining stale files in an archive.

## Tox and CI

```console
tox -e docs
tox -e docs-pdf
```

`docs` builds both HTML layouts and is part of the default tox environment list.
`docs-pdf` additionally builds the PDF and is opt-in because it requires TeX.
Both environments use the `dev` extra. Mermaid CLI and TeX remain external
prerequisites; tox does not install them. To forward a system-browser override,
set `PUPPETEER_EXECUTABLE_PATH` before invoking tox.

Builds enable nitpicky references and fail on warnings. A CI job can run these
commands and publish `_build/html/` and the PDF as artifacts. No CI or hosting
configuration outside `doc/` is introduced here.

## Maintain the sources

- Add new chapters to the appropriate toctree in `index.md`.
- Keep internal links relative to the Markdown file and use stable heading anchors.
- Include configurations with `literalinclude` rather than copying their contents.
- Use download roles for files to distribute; never copy all of `examples/`,
  which can contain local session logs.
- Keep diagram fences marked `mermaid`; the build produces static images for both formats.
- Prefer prerequisites, commands, expected results and limitations before implementation details.

The document version is read from installed package metadata, falling back to
`development` when the package is not installed. External source-code links are
supplementary and are not required to read the guide offline. Live hardware
examples and external URL availability are not tested by the Sphinx build.
