<!--
SPDX-FileType: DOCUMENTATION
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# Python implementation guidelines

These guidelines apply to Python code in `src/` and `tests/`. Preserve existing
public behavior unless a task explicitly requires a change.

## Runtime and style

- Support Python 3.12 and newer. Do not rely on syntax or runtime behavior
  unavailable on the minimum supported version.
- Format with Black using the project configuration (100 columns). When Black
  and Flake8 disagree, follow Black; keep code compliant with Flake8 otherwise.
- Follow the existing package and test layout rather than introducing
  parallel implementations or unnecessary abstractions.

## Types and documentation

- Add precise annotations to function parameters, return values, and data
  structures. Keep `src/` clean under the configured strict Mypy check; prefer
  concrete types and validated boundaries over avoidable `Any` or casts.
- Do not use `from __future__ import annotations` or quoted annotations.
  Annotations must evaluate on Python 3.12 and 3.13; use `Self`, `Protocol`,
  or definition ordering for forward references.
- Use NumPy-style docstrings for documented APIs, including parameter and
  return types, and document relevant exceptions.
- Decorate `IntEnum` and `StrEnum` classes with `@unique` unless aliases are
  intentional. Use `auto()` for `StrEnum` members when the serialized value
  should be the lowercase member name.

## Architecture and failure handling

- Keep architecture-neutral command entry points architecture-neutral. Extend
  architecture behavior through the existing probe and service registries
  instead of adding architecture-specific branches to generic commands.
- `secscan` and `fault_info` dispatch through `DEFAULT_DIAGNOSTIC_RUNTIME`.
  Import Arm implementation APIs from their defining `arch.arm` modules;
  `arch.arm` intentionally exposes only its small integration API.
- Represent unavailable results and access failures explicitly. Catch errors
  narrowly and translate them at API boundaries; do not hide failures behind
  silent defaults or success-shaped fallbacks.

## Tests and checks

- Add or update pytest tests for behavior changes, including relevant failure
  and boundary cases. Keep tests independent of live GDB hardware where
  practical, following the existing fakes and fixtures.
- Run the relevant Tox environments before considering a Python change done:
  `tox -e py312` for toolkit/server tests, `tox -e client` for client tests,
  `tox -e lint` for Black and Flake8, and `tox -e typecheck` for Mypy.
