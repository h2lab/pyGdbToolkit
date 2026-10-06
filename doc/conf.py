from importlib.metadata import PackageNotFoundError, version as package_version

from pygments.lexers import TextLexer

project = "pyGdbToolkit"
author = "H2Lab Development Team"
try:
    release = package_version(project)
except PackageNotFoundError:
    release = "development"
version = release

extensions = ["myst_parser"]
root_doc = "index"
language = "en"
exclude_patterns = [
    "_build", "README.md", "node_modules", "examples/**/.pygdbserver-logs/**",
    "diagrams/*.mmd",
]
myst_heading_anchors = 4
myst_enable_extensions = ["colon_fence"]

html_theme = "furo"
html_title = f"{project} {release} User Guide"
html_logo = "logo.png"
html_copy_source = False
html_show_sourcelink = False

latex_engine = "pdflatex"
latex_logo = "logo.png"
latex_documents = [("index", "pyGdbToolkit-user-guide.tex", html_title, author, "manual")]
latex_elements = {
    "papersize": "a4paper",
    "pointsize": "10pt",
    "preamble": r"\setlength{\headheight}{24pt}",
}


def setup(app):
    app.add_lexer("gdb", TextLexer)
