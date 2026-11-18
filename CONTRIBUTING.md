<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# Contributing to pyGdbToolkit

Thank you for considering a contribution! Contributions are accepted through
GitHub [issues](https://github.com/h2lab/pyGdbToolkit/issues) and
[pull requests](https://github.com/h2lab/pyGdbToolkit/pulls). By participating
you agree to follow the [code of conduct](CODE_OF_CONDUCT.md), and your
contributions are licensed under [Apache-2.0](LICENSE).

## Reporting issues

Before opening an issue, search the existing ones to avoid duplicates. A clear
issue states one problem or one request, with a descriptive title.

For bug reports, always include how to reproduce the problem:

- the pyGdbToolkit version (`pip show pyGdbToolkit` or the git commit),
- the Python and GDB versions, the host OS, and the debug probe or QEMU setup,
- the target (SoC/board) and the relevant configuration file, redacted if needed,
- the exact commands or API calls that trigger the problem,
- the expected behavior, the observed behavior, and the full error output or
  logs (for server sessions, the relevant `.pygdbserver-logs` excerpts).

For feature requests, describe the use case and the expected behavior rather
than only a solution. For suspected security vulnerabilities, do **not** open a
public issue; follow the [security policy](SECURITY.md) instead.

## Submitting pull requests

1. Fork the repository and create a topic branch from `main`.
2. Keep each pull request focused on a single change; split unrelated work.
3. Follow the project conventions:
   - Python 3.12+ compatible code, formatted with Black (100 columns) and
     clean under Flake8; when they disagree, Black wins.
   - Precise type annotations; `src/` must stay clean under strict Mypy.
   - NumPy-style docstrings for documented APIs.
   - [REUSE](https://reuse.software/)-compliant SPDX headers on new files.
4. Add or update tests for any behavior change, including failure and boundary
   cases. Tests must not require live GDB hardware unless they belong to the
   explicitly opt-in hardware suites.
5. Update the documentation in `doc/` and the README when behavior, commands,
   or configuration change.
6. Write clear commit messages explaining the *why* of the change.

### Required checks

Run the relevant Tox environments locally before submitting:

```console
tox -e py312     # GDB-side toolkit and server tests (fake gdb module)
tox -e client    # dashboard and LLM client tests
tox -e lint      # Black and Flake8
tox -e typecheck # strict Mypy on src/
tox -e docs      # only when touching the documentation
```

The same checks run in CI together with the REUSE compliance check, and they
must pass before a pull request can be merged.

### Test coverage

Both test environments report branch coverage. Pull requests must maintain the
existing test coverage: new or modified code paths need accompanying tests, and
a change that lowers coverage without justification will be asked to add tests
before review proceeds.

## Review process

Maintainers review pull requests as time permits. Be responsive to review
comments, push follow-up commits to the same branch, and avoid force-pushing
once a review has started unless asked to rebase. Small, well-tested, and
well-described pull requests are merged fastest.
