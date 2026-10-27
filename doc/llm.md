<!--
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# LLM integration contracts

## Status and scope

The first LLM-support increment defines provider-neutral Python contracts and a
local scripted provider in `pyGdbClient.llm`. It does **not** enable an agent in
the dashboard, contact a model, collect target data, or execute commands.
The existing command prompt, RPC protocol, and dashboard behaviour are unchanged.
No new dependency is required.

The second increment isolates client tests from GDB (see
[Client test suite](#client-test-suite)). Subsequent increments will add a
structured target-context RPC, SQLite persistence, a tool catalogue, target
protection, auditor approval, orchestration, the Copilot adapter, and the second
dashboard prompt. Long-context management and end-to-end simulated audit
workflows follow.

## Trust boundaries

The intended path is:

```text
Auditor prompt -> orchestrator -> LLM provider
                                      |
                              untrusted proposals
                                      |
                local policy -> auditor approval when required
                                      |
                            bounded target executor
                                      |
                           existing JSON-RPC transport
```

The provider receives context and tool **definitions**, never executable
callbacks, a target-memory accessor, or an RPC client. A `ToolCallProposed` event
does not execute anything. Only a future local executor may dispatch an operation,
after argument validation, policy assessment, and any required auditor approval.

`ActionPlan` identifies the target-context snapshot and the ordered steps.
`ActionEffect` describes possible effects; omitted effects default to `unknown`.
Effects claimed by a model are untrusted: the local catalogue and policy must
classify the actual operation and its arguments independently.

`TargetPolicy.evaluate()` assesses the complete plan against a current snapshot.
`PolicyDecision` has one of three explained outcomes: `allow`,
`require_approval`, or `deny`. None of these objects is an auditor approval token.
This increment supplies the interface, not an allowing policy implementation.

Frozen dataclasses prevent field reassignment but JSON dictionaries and lists
inside tool payloads remain mutable. They are data-transfer objects, not signed
or immutable authorization records. The future approval/execution layer must
snapshot the exact validated arguments, bind approval to their canonical digest
and target context, and recheck them before dispatch. A changed plan or stale
target context must not inherit a previous approval.

Reads of MMIO can change hardware state, including clearing status bits or
consuming FIFO data. A declared memory range or SVD description is not proof of
safe access. Raw GDB/OCD commands, scripts, unknown operations, persistent writes,
and execution-control changes require conservative classification. Prompt
instructions alone cannot protect the equipment.

Stopping a model turn does not undo a hardware operation. Later execution must
report interrupted or uncertain outcomes and must not automatically retry
side-effecting operations after a timeout.

## Target context

`TargetContext` schema version 1 carries a snapshot ID and:

- MCU/SoC name and revision;
- architecture, debug transport (for example `jtag` or `swd`), and OCD identity;
- selected core;
- declared half-open memory ranges with their kind and source;
- the loaded SVD file path, when available.

`ContextFact` distinguishes a sourced value from an unavailable value with an
explicit reason. `MemoryMapContext` likewise distinguishes a missing map from an
available, possibly empty map. A snapshot ID identifies a particular observation
context; it is not just the MCU name. The collector must refresh it when relevant
target state or configuration changes.

SVD paths refer to the **server** filesystem. A path is not the SVD contents and
must not be opened on the client as though it were local. The context collector
and controlled retrieval of SVD metadata/content belong to subsequent increments.
This layer imports neither GDB nor toolkit implementation modules.

## Provider lifecycle and events

`LlmProvider` exposes:

1. `provider_id`: a stable implementation identifier.
2. `status()`: readiness, authentication state, and sanitized guidance. It must
   not initiate authentication or return credentials.
3. `open_session(session_id, context, tools)`: an isolated conversation given
   an application-owned ID, an explicit context snapshot, and tool schemas.

`LlmSession.stream()` accepts either a `UserPrompt` or correlated `ToolResults`.
It yields ordered `TextDelta` and complete `ToolCallProposed` events, followed
by exactly one `TurnFinished`. The terminal reason is `tool_calls` when the turn
proposes invocations, otherwise `completed`. Partial tool-call JSON is assembled
and validated by adapters before it is exposed to the orchestrator.

The orchestrator collects a complete turn before executing proposals. It
correlates tool results by call ID, and enforces the advertised schemas, call
ordering, and protection policy. Provider output is never a direct RPC command.
Provider errors raise `ProviderError`; failure or truncated streaming must not
be converted into a successful terminal event.

Only one turn may stream per session. `cancel()` interrupts an active turn;
an idle cancel does not affect the next turn. `close()` is idempotent and rejects
subsequent input. Callers must fully consume or close an iterator they abandon;
exceptions and task cancellation must still release the stream's resources.
Credentials and provider-specific login flows stay outside conversation objects,
tool arguments, logs, and future SQLite history.

## Scripted provider

`ScriptedProvider` replays a fixed tuple of event turns. Each opened session gets
an independent copy of the script and tool payloads. It validates terminal
events, tool-call finish reasons, and per-turn call-ID uniqueness. Script
exhaustion raises an error rather than synthesizing a reply.

The scripted session records copied inputs for assertions and yields control
between events so cancellation can be exercised deterministically. Cancelling
or abandoning a turn consumes that scripted turn; it is not silently replayed.
It performs no network, filesystem, target, or tool execution.

The contract tests belong to the client suite, which runs without GDB, the
toolkit, the server, or a target:

```console
tox -e client
```

The suite's import guard rejects `gdb`, `pyGdbToolkit`, and `pyGdbServer`, and
refuses to start if any is already loaded, rather than treating a fake GDB
module as evidence of independence. A fresh-interpreter test also rejects
`copilot`.

## Client test suite

`tests_client/` holds the dashboard and LLM tests. It must run in its own pytest
process, separate from the GDB-side suite in `tests/`, because that suite
installs a fake `gdb` module at import time. Tox runs both in separate
environments (`py312` and `client`).

`tests_client/rpc_fakes.py` provides `FakeRpcServer`, a loopback WebSocket
JSON-RPC double. Each method is scripted as a fixed result, an `RpcFailure`, or
a callable receiving the request parameters; unscripted methods return
`-32601`, so unexpected calls fail visibly. It records every request in arrival
order and can broadcast notifications. Provider behaviour is scripted with
`ScriptedProvider`.

## Copilot adapter feasibility

The official SDK documentation and Python manifest were inspected for the first
increment, at upstream revision
`26536883459ba4cb23c735db8cb3de2c7b899d88`:

- [Python SDK and runtime documentation](https://github.com/github/copilot-sdk/blob/26536883459ba4cb23c735db8cb3de2c7b899d88/python/README.md)
- [Python package requirements](https://github.com/github/copilot-sdk/blob/26536883459ba4cb23c735db8cb3de2c7b899d88/python/pyproject.toml)
- [Authentication methods](https://github.com/github/copilot-sdk/blob/26536883459ba4cb23c735db8cb3de2c7b899d88/docs/auth/authenticate.md)

The package is `github-copilot-sdk`, imported as `copilot`. Its manifest requires
Python 3.11 or newer, compatible with this project's Python 3.12 minimum.
The inspected manifest describes an alpha SDK. Its documented runtime uses
JSON-RPC; published wheels provide a pinned runtime version with a separate
download/provisioning step, potentially performed automatically on first use.
Installation, runtime availability, platform support, and offline provisioning
must therefore be diagnosed explicitly by the eventual optional adapter.

Documented authentication includes existing Copilot CLI login credentials and
explicit GitHub tokens through `github_token` / `use_logged_in_user`. Interactive
CLI login uses GitHub's OAuth device flow. Subscription and organization policy
requirements depend on the authentication mode. The adapter should initially
guide the auditor through supported CLI authentication and report required or
expired authentication without handling passwords or persisting tokens itself.

The SDK documents custom tools, streaming, session lifecycle methods,
`available_tools` / `excluded_tools`, and `on_permission_request`. The inspected
API also documents declaration-only tools and an empty client mode. These are
integration mechanisms, **not** a substitute for pyGdbClient's target policy.
The adapter must expose only approved application tools, deny unexpected native
tools, and prevent ambient shell, file, MCP, skill, or subagent capabilities from
bypassing the executor. It must not use an approve-all permission handler.
Creation and resumption must both enforce these restrictions.

Before implementing that adapter, select and pin a released SDK/runtime pair
and verify these controls against that release. This source inspection does
not certify a released version, authenticate a real account, or test a live
model. No SDK package or runtime is installed by this increment.
