"""Render versioned Mermaid sources to PNG before a local documentation build."""

from pathlib import Path
import shutil
import subprocess


def main() -> None:
    executable = shutil.which("mmdc")
    if executable is None:
        raise SystemExit("Mermaid CLI is required: npm install --global @mermaid-js/mermaid-cli@11")
    for source in sorted(Path(__file__).parent.joinpath("diagrams").glob("*.mmd")):
        subprocess.run(
            [
                executable,
                "--input", str(source),
                "--output", str(source.with_suffix(".png")),
                "--backgroundColor", "white",
                "--width", "1200",
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
