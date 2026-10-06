# Building the user guide

The Markdown files in this directory are the shared source for HTML and PDF.
`index.md` defines the reading order. Sphinx uses MyST Parser and the Furo HTML
theme; no toolkit module is imported by the build.
The guide stays in English, matching the existing chapters.

## Install the build tools

From the repository root, in a Python 3.12+ virtual environment:

```console
python -m pip install tox
```

Tox creates an isolated environment and installs the documentation dependencies
from the project's `dev` extra.

Tox renders Mermaid sources to versioned PNG images before building HTML or PDF.
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
tox -e docs
tox -e docs-pdf
```

To generate both HTML and PDF in one invocation, use `tox -e docs,docs-pdf`.
Outputs relative to this directory are:

| Target | Output |
| --- | --- |
| `docs` | `_build/html/index.html`, navigation, search, images and downloads |
| `docs` | `_build/singlehtml/index.html`, a continuous guide with local assets |
| `docs-pdf` | `_build/latex/pyGdbToolkit-user-guide.pdf` |

HTML outputs include local assets and downloadable example configurations.
Keep the complete output directory when opening `index.html`; the monopage output
is not a single resource-embedded HTML file. No web server is required.
The PDF contains the generic configuration listings, but downloadable board
files are available in the HTML distribution only.

Use `make -C doc clean` to delete only generated documentation output. Clean
before a release build to avoid retaining stale files in an archive.

## CI and distribution archives

`docs` builds both HTML layouts and is part of the default tox environment list.
`docs-pdf` additionally builds the PDF and is opt-in because it requires TeX.
Both environments use the `dev` extra. Mermaid CLI and TeX remain external
prerequisites; tox does not install them. To forward a system-browser override,
set `PUPPETEER_EXECUTABLE_PATH` before invoking tox.

Builds enable nitpicky references and fail on warnings. A CI job can run these
commands and publish `_build/html/` and the PDF as artifacts. Read the Docs runs
Sphinx directly using the committed PNG images: it needs neither Node.js,
Mermaid CLI nor Chromium.

Read the Docs does not invoke tox or `render_diagrams.py`. Its Sphinx build
depends only on the committed PNG assets, never on `diagrams/*.mmd`. A direct
offline HTML build uses the same path without regenerating diagrams:

```console
python -m sphinx -b html -n -W --keep-going doc doc/_build/html
```

For optional ZIP archives and a PDF copy directly under `_build/`, install the
`dev` extra in your active environment and use the packaging target:

```console
python -m pip install -e '.[dev]'
make -C doc dist
```

Set `PYTHON=/absolute/path/to/python` for Make if the intended interpreter is not
`python3`. HTML archives contain all local resources; extract the full archive
before opening `index.html`.

## Maintain the sources

- Add new chapters to the appropriate toctree in `index.md`.
- Keep internal links relative to the Markdown file and use stable heading anchors.
- Include configurations with `literalinclude` rather than copying their contents.
- Use download roles for files to distribute; never copy all of `examples/`,
  which can contain local session logs.
- Edit Mermaid sources in `diagrams/*.mmd`, then run `tox -e docs` or
    `tox -e docs-pdf` to regenerate every PNG through `render_diagrams.py`.
- Commit each changed Mermaid source together with its regenerated PNG.
    Markdown chapters reference `diagrams/*.png` directly; Sphinx does not render Mermaid.
- Use PNG for every illustration and workflow diagram in Markdown. Do not embed
    Mermaid fences, include `.mmd` sources, or use SVG diagrams. Command examples
    and literal terminal output remain text, not images.
- Prefer prerequisites, commands, expected results and limitations before implementation details.

The document version is read from installed package metadata, falling back to
`development` when the package is not installed. External source-code links are
supplementary and are not required to read the guide offline. Live hardware
examples and external URL availability are not tested by the Sphinx build.
