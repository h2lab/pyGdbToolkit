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
[Client test suite](#client-test-suite)). The third adds the structured
[`target.context`](#target-context) RPC. The fourth adds local SQLite
[conversation persistence](#conversation-persistence). The fifth supplies the
[tool catalogue and controlled RPC gateway](#tool-catalogue-and-rpc-gateway).
The sixth adds the [deterministic protection policy](#deterministic-protection-policy).
The seventh adds [one-attempt auditor approval](#auditor-approval) and its Textual modal.
The eighth adds the bounded [agent orchestrator](#agent-orchestrator).
The ninth adds the optional [GitHub Copilot provider](#github-copilot-provider).
The tenth adds the [dashboard agent CLI](#dashboard-agent-cli), a second prompt
that routes natural-language requests through the orchestrator, the policy and
auditor approval, with output in the central console. Long-context management
and end-to-end simulated audit workflows follow.

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
does not execute anything. Only the local execution gateway may dispatch an operation,
after argument validation, policy assessment, and any required auditor approval.

`ActionPlan` identifies the target-context snapshot and the ordered steps.
`ActionEffect` describes possible effects; omitted effects default to `unknown`.
Effects claimed by a model are untrusted: the local catalogue and policy must
classify the actual operation and its arguments independently.

`TargetPolicy.evaluate()` assesses the complete plan against a current snapshot.
`PolicyDecision` has one of three explained outcomes: `allow`,
`require_approval`, or `deny`. None of these objects is an auditor approval token.
`ProtectionPolicy` supplies the conservative local implementation described below.

Frozen dataclasses prevent field reassignment but JSON dictionaries and lists
inside tool payloads remain mutable. They are data-transfer objects, not signed
or immutable authorization records. The approval/execution layer snapshots
the exact validated arguments, binds approval to their canonical digest
and target context, and rechecks them before dispatch. A changed plan or stale
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
must not be opened on the client as though it were local. Controlled retrieval
of SVD metadata/content belongs to subsequent increments.
This layer imports neither GDB nor toolkit implementation modules.

The snapshot is served by the [`target.context`](pygdbserver.md#targetcontext)
RPC. Collection is split in three layers:

1. `pyGdbToolkit.target_context.collect_target_context()` runs inside GDB and
   reads only established session state (cached probe, confirmed discovery,
   SVD, selected core) and host-side GDB state. It never probes the target.
2. `pyGdbServer.target_context.build_target_context()` validates those facts,
   adds the OCD identity and the debug transport, applies the declared
   `target` configuration metadata, and derives `context_id` from the content.
3. `pyGdbClient.llm.context.fetch_target_context()` requests the snapshot and
   `parse_target_context()` decodes it strictly into `TargetContext`. A missing
   field, a contradictory fact, an invalid region, or a schema version other
   than 1 raises `TargetContextError`; unknown extra keys are ignored.

Memory regions keep their optional `name` and `protection` metadata.

## Conversation persistence

`pyGdbClient.llm.store.ContextStore` is a synchronous, provider-independent
SQLite journal. It imports neither GDB, the toolkit, the server, nor a provider
SDK. This increment supplies the storage API; automatic recording, provider
resumption and dashboard controls will be wired by the orchestrator and dashboard
increments.

The store records:

- Sessions with application-owned IDs, provider IDs, titles, UTC creation times,
  and the current target-context ID.
- Target snapshots, retaining all `TargetContext` fields and their provenance.
- Ordered history entries of kind `user`, `assistant`, `tool_call`, `tool_result`,
  `summary`, or `error`, with JSON-object payloads, UTC timestamps, and a
  session-local sequence starting at 1.

Every entry refers to a snapshot registered in the same session. Registering a
new snapshot with `save_context()` selects it for subsequent entries but does
not rewrite earlier observations. A late result can explicitly specify its
original `context_id`. Reusing an ID with different facts raises `StoreError`;
identical snapshots can be registered again. The same context ID in separate
sessions does not share or overwrite their snapshots.

```python
from pyGdbClient.llm.store import ContextStore, EntryKind

# context is a validated TargetContext obtained from target.context.
with ContextStore("audit.sqlite3") as store:
    store.create_session("audit-1", "scripted", context, title="GPIO audit")
    store.append("audit-1", EntryKind.USER, {"text": "Inspect the GPIO state"})
    # Later, after the executor returns an observation:
    store.append(
        "audit-1",
        EntryKind.TOOL_RESULT,
        {"call_id": "read-1", "content": "GPIO unchanged", "is_error": False},
        context_id=context.context_id,
    )

# Reopening the file preserves sessions, target snapshots and ordering.
with ContextStore("audit.sqlite3") as store:
    session = store.get_session("audit-1")
    saved_context = store.get_context("audit-1")
    entries = store.history("audit-1", after_sequence=0, limit=100)
```

`list_sessions(limit, offset)` and `history(session_id, after_sequence, limit)`
provide bounded pagination (1 to 1000 records per page). The history cursor is
exclusive: use the last returned sequence to fetch the next page. Payloads are
copied through strict JSON serialization; mutating an input or a returned entry
cannot modify persisted data. Non-string object keys, non-finite numbers and
non-JSON objects are rejected.

The database has an application identifier and schema version 1. Only an empty,
unversioned database is initialized. Foreign, nonempty unversioned, or unsupported
version databases are rejected without modification; migrations must be added
explicitly when the schema changes. Session creation, snapshot registration,
entry insertion and session deletion are transactional. Foreign keys bind history
to snapshots and cascade deletion. Concurrent connections serialize writes before
allocating sequence numbers. Each connection must stay in its creating thread;
future async callers should keep blocking storage work outside the dashboard's
event loop.

Unknown sessions/snapshots raise `KeyError`, invalid API inputs raise `ValueError`,
and incompatible schemas, conflicting snapshot IDs or malformed stored content
raise `StoreError`. SQLite I/O, lock, integrity and corruption errors propagate;
there is no silent fallback to an empty or in-memory journal. Closing is
idempotent, and operations after closing fail explicitly.

### Persistence trust boundary

The caller supplies the path; the parent directory must already exist and should
be private to the auditor. No database is created at import time or implicitly
by the existing dashboard. The store captures no authentication state, environment
variables or SDK objects, and emits no payload logs. Its payloads are explicit
caller-supplied JSON: callers must never include credentials, and must apply
the future confidentiality rules before storing sensitive target observations.

SQLite files are **not encrypted**. `delete_session()` logically deletes the
session, its snapshots and its history, but does not guarantee forensic erasure
from files, journals or backups. Summary payloads are stored observations, not
automatic compaction: this increment neither deletes covered history nor chooses
what a model sees.

Restored history, tool proposals and results are **not authorization to execute
or replay operations**. After reopening, the orchestrator must fetch a fresh live
target context and reassess plans against the protection policy and auditor
approval. A persisted snapshot alone cannot prove that the same equipment is
still connected. Session restoration does not resume a Copilot SDK conversation;
that remains the responsibility of the provider adapter and orchestrator.

## Tool catalogue and RPC gateway

`pyGdbClient.llm.tools.ToolCatalogue` advertises provider-neutral `ToolDefinition`
objects with JSON Schema object arguments (`additionalProperties: false`).
Definitions contain no callbacks or transport. The same local parameter
descriptors generate the schemas and validate primitive types, required fields
and limits. `prepare()` rejects unknown tools/arguments and returns a detached
`PreparedCall`: the normalized arguments, fixed RPC route, exact parameters,
timeout and local effects. Preparing a call is **not authorization** to run it.

| Tool | RPC route or generated command | Local effects |
|---|---|---|
| `target_context` | `target.context` | observe |
| `target_status` | `target.status` | observe |
| `target_cores`, `target_core` | `target.cores`, `target.core` | observe |
| `target_select_core` | `target.select_core` with integer `core` | execution control, unknown |
| `svd_peripherals` | `svd.peripherals` (loaded metadata only) | observe |
| `toolkit_commands`, `toolkit_help` | `toolkit.commands`, `toolkit.help` | observe |
| `register_names` | MI `-data-list-register-names` | observe |
| `register_values` | MI `-data-list-register-values x` | observe |
| `memory_read` | MI `-data-read-memory-bytes <address> <length>` | read memory |
| `toolkit_command` | `command.execute`, unmodified command | unknown |
| `gdb_command` | `command.execute`, `gdb ` prefix | unknown |
| `ocd_command` | `command.execute`, `monitor ` prefix | unknown |
| `mi_command` | `mi.execute`, command must start with `-` | unknown |

`memory_read` accepts a JSON integer address and length, rejects booleans and
floats, and limits each read to 1-4096 bytes without overflowing the 64-bit
address domain. This limit is not permission to read MMIO. All tools have an
optional `timeout` in seconds (0.001-300, default 30); the client waits five
additional seconds for the response. The parameter is forwarded to existing
RPC methods; methods without server-side timeout support still use their
existing server timeout. Command strings are nonblank, at most 4096 characters,
and exclude control characters, including newlines. Toolkit help names are
limited to 128 characters. Core IDs are integers from 0 to `2**31 - 1`.

The free-command tools retain the capabilities of GDB/OCD/toolkit, including
memory writes, watchpoints, execution control and toolkit analyses. They are
**not sandboxes or restricted command languages**: GDB can also run scripts,
access host files and execute host commands. Even `toolkit_command` is resolved
by GDB and does not guarantee that the command belongs to the toolkit. These
tools are always classified as unknown, irrespective of the command name or
model's claimed effects. Shell/file/MI escape routes must never be treated as
safe by string matching. Arbitrary RPC methods, workspace transfers, shutdown
and log subscriptions are not exposed by the catalogue.

`pyGdbClient.llm.gateway.ToolGateway.execute(plan, context)`:

1. Validates **every** proposed call before any RPC or policy evaluation.
2. Builds a detached normalized plan, replacing model-supplied effect labels
   with catalogue-owned classifications and making default arguments explicit.
3. Rejects a plan bound to a different supplied target-context ID.
4. Asks the application-owned `TargetPolicy` to assess the **whole** plan.
   No configured policy and `deny` always block dispatch. `require_approval`
   blocks unless an application-owned auditor callback returns matching,
   explicit consent before its deadline.
5. Before **each** operation, fetches the live target context and checks that
   both its ID and facts equal the supplied snapshot. A mismatch or unavailable
   context blocks the remaining steps. It also reassesses the entire plan and
   blocks if the policy decision or explanation changed since review.
6. Dispatches sequentially under a per-gateway lock. Arguments cannot be changed
   by mutating the original proposal after validation. The first failed
   operation stops the chain, with no automatic retry.

Effects describe operation classes, not an unconditional safe-read policy.
The [deterministic policy](#deterministic-protection-policy) is available in
`pyGdbClient.llm.policy`; the auditor callback is described below.
Neither the store nor the provider receives this gateway or its transport.

Results are correlated `ToolResult` objects whose `content` is valid JSON.
Normal content is `{"result": <RPC object>, "truncated": false}`. The complete
encoded content is capped at **64 KiB**; larger results become
`{"preview": "...", "original_bytes": N, "truncated": true}`. A preview is an
incomplete serialized observation, not a complete structured result. GDB/MI
results are checked for their `class`, `record` and string-array `output`.
GDB errors and malformed responses set `is_error`. Transport failures produce
an error observation with `outcome: "unknown"`: the target action may already
have run. Long error text is also explicitly truncated within the byte limit.

Validation errors raise `ToolValidationError`. Context/policy blocks raise
`GatewayError`, retaining any earlier results in `results`. Policy blocks also
carry the structured `PolicyDecision` in `decision`, so callers need not parse
the exception message. Other blocks leave `decision` as `None`. Cancellation raises
`GatewayCancelledError` (an `asyncio.CancelledError`) with prior `results` and a
`pending_call_id` when an operation may still be in flight. None of these
outcomes rolls back target actions. The caller must not silently replay
unexecuted or uncertain steps.

The live-context check and execution are separate RPCs: the gateway serializes
its own plans, **not other dashboard clients or standalone GDB users**. It does
not provide an atomic check-and-execute operation across clients. A deliberate
core switch also changes context: subsequent actions need a fresh snapshot and
new assessment, rather than inheriting the old plan. Asynchronous target output
still arrives through existing log notifications, outside an individual RPC
result; collecting it for the agent belongs to the orchestrator increment.

## Deterministic protection policy

`pyGdbClient.llm.policy.ProtectionPolicy` implements `TargetPolicy`. It is a pure
client-side assessment: no RPC, GDB import, target probe, provider request or
filesystem access is performed. It validates proposals again through the local
catalogue and ignores model-supplied effect labels, rationale and claims of
previous approval.

The default rules are:

| Operation | Decision | Reason |
|---|---|---|
| `target_context`, `target_status`, `target_cores`, `target_core`, `svd_peripherals`, `toolkit_commands`, `toolkit_help`, `register_names` | `allow` | Explicit metadata allowlist, with the expected local `observe` classification. |
| `memory_read` | `require_approval` | Any target memory read may have side effects, even with a declared RAM/ROM/flash region and read permission. |
| `register_values` | `require_approval` | Fetches target values rather than register catalogue metadata. |
| `target_select_core` | `require_approval` | Attaching may halt the core; selection changes context for later steps. |
| Free toolkit/GDB/OCD/MI commands | `require_approval` | Opaque command languages may alter memory, execution, flash/OTP, debug protection or the host filesystem/processes. Text matching cannot prove safety. |
| Unknown tools, invalid arguments or operations without an explicit policy rule | `deny` | A new catalogue operation does not inherit blanket permission from an `observe` label. |
| Plan bound to a different supplied target-context ID | `deny` | A fresh plan is required. |

Memory-map kinds, access permissions and provenance are **evidence to show the
auditor, not authorization**. Memory-read explanations include the requested
half-open interval, all overlapping declared regions and their sources and
protections. An unavailable map, addresses outside declared ranges, and reads
not contained within one region receive explicit explanations. Overlays are
retained: a broad RAM declaration cannot hide a narrower register window.
Adjacent regions or gaps do not imply that a combined read is safe.

Applications can configure non-overridable local prohibitions:

```python
from pyGdbClient.llm.contracts import MemoryRegion
from pyGdbClient.llm.gateway import ToolGateway
from pyGdbClient.llm.policy import ProtectionPolicy

policy = ProtectionPolicy(
    denied_tools={"target_select_core"},
    denied_memory=(
        MemoryRegion(
            0x40000000, 0x40001000,
            kind="protected", source="auditor", name="Sensitive control window",
        ),
    ),
)
gateway = ToolGateway(client, policy)
assessment = policy.assess(plan, context)
decision = policy.evaluate(plan, context)
```

`denied_tools` accepts only known catalogue names; configuration typos fail
explicitly. `denied_memory` contains `MemoryRegion` objects with integer bounds.
For matching, their kind/protection labels do not change the prohibition:
**any byte** shared by a `memory_read` and a configured half-open range yields
`deny`, including when the live map is unavailable or labels it as ordinary RAM.
A read ending exactly at the range start or beginning at its end does not
overlap. Configuration is copied, deduplicated and is never read from model
arguments. Local rules must be chosen by the application/auditor for the
equipment being assessed.

When **any** local tool or address prohibition is configured, **all opaque
command tools are denied**, not merely sent for approval. Their scripts and
alternate command spellings could otherwise bypass a prohibited structured
operation. Supporting a constrained operation under these restrictions requires
an explicit validated tool and corresponding policy rule, not a command-text
allowlist. These are client execution rules, not a server-side memory sandbox:
they do not constrain other clients or the debug server's own internal accesses.

`assess()` returns a `PlanAssessment` with ordered `ActionAssessment` objects.
Each action contains its call ID, tool name, catalogue-owned effects and
explained `PolicyDecision`. The aggregate uses the strict precedence
**`deny` > `require_approval` > `allow`** and preserves every action's reasons.
It assesses the whole chain: a safe prefix is not dispatched before an unsafe
or prohibited later step is resolved. `evaluate()` returns that aggregate
decision for the existing gateway protocol.

Assessment objects and `require_approval` are **not auditor consent**. Without
an auditor callback, the gateway still blocks both `deny` and `require_approval`.
There is no approved flag, persisted authorization or allowing fallback.
Its no-policy default also remains disabled. Applications must explicitly
supply `ProtectionPolicy` to enable its metadata-only automatic path.
Local restrictions are not automatically persisted in conversation history;
the application must reapply them when reopening an audit.

The gateway's live-context refresh remains mandatory, including after consent,
and retains its documented multi-client check/execute limitation.

## Auditor approval

`pyGdbClient.llm.approval` defines immutable `ApprovalRequest` and
`ApprovalResponse` objects. For `require_approval`, the gateway creates a new
request **inside its execution lock**, before dispatching even a metadata prefix.
The request contains a canonical, detached JSON snapshot of:

- the complete ordered plan, its ID, rationale and each step's purpose;
- validated arguments with defaults and catalogue-owned effects, never model risk labels;
- exact RPC methods, parameters and timeouts;
- the complete target context (identity, facts and provenance);
- the local policy outcome and reasons.

A SHA-256 digest binds all these data. A unique request ID additionally binds
consent to **one execution attempt**, even if the plan and digest are identical
to a previous attempt. Both must match in the response, and `approved` must be
explicitly `True`. Mutating the original proposal after validation changes
neither the displayed snapshot nor the dispatched calls.

The callback is an **application-owned trust boundary**, not an LLM tool. The
digest is an integrity binding, not a signature authenticating the auditor.
Only the local UI may return consent; providers must not receive the callback,
the gateway, or its transport.

For the dashboard, wire the existing application instance as follows:

```python
from pyGdbClient.llm.gateway import ToolGateway
from pyGdbClient.llm.policy import ProtectionPolicy

gateway = ToolGateway(
    app.client,
    ProtectionPolicy(),
    approver=app.request_plan_approval,
    approval_timeout=120.0,
)
# A later orchestrator calls await gateway.execute(plan, context).
```

`PyGdbClientApp.request_plan_approval()` opens `ApprovalScreen`, displaying the
entire snapshot in a scrollable modal. All proposed text is literal, without
Rich markup or ANSI interpretation. **Reject** has initial focus: Enter rejects,
Escape rejects, and only the explicit **Approve entire chain** button approves.
Request/decision messages use the central output console. Overlapping dashboard
dialogs fail explicitly rather than mixing two auditor decisions.

The default deadline is **120 seconds** and must be finite and positive.
Rejection, mismatch and expiry raise `GatewayError` before any plan RPC.
Cancellation closes the modal without consent; closing the dashboard cancels a
pending request. Once the auditor responds, the gateway verifies the live
context and reassesses policy before every action. A changed context or changed
policy decision requires a fresh plan/assessment, never an inherited approval.
A `deny` decision cannot be overridden by consent.

There are no stored approval tokens, session-wide allowances or automatic
replay. A repeated execution always requests new consent. Conversation history,
restored plans and old `ApprovalResponse` objects cannot authorize a new request.
Failure or cancellation during dispatch retains the gateway's partial-results
and uncertain-in-flight semantics; consent does not imply rollback or safe retry.

The approval API and modal do not create a gateway automatically in the
dashboard; the [orchestrator](#agent-orchestrator) drives them, and the second
CLI wires everything together in a later increment.

## Agent orchestrator

`pyGdbClient.llm.orchestrator.AgentOrchestrator` runs one auditor request at a
time between an `LlmSession` and a `ToolGateway`:

```python
from pyGdbClient.llm.orchestrator import AgentBudget, AgentOrchestrator

agent = AgentOrchestrator(
    session,                 # from provider.open_session(...)
    gateway,                 # ToolGateway(client, ProtectionPolicy(), approver=...)
    context,                 # snapshot sent when the session was opened
    sink=render_event,       # synchronous callback for the central console
    budget=AgentBudget(),
    store=store,             # optional ContextStore with an existing session
    context_client=client,   # optional; refreshes the snapshot after context errors
)
outcome = await agent.submit("Why does the UART driver lose bytes?")
```

The provider only receives its session. The gateway, transport, approval
callback and store stay local. For each request the orchestrator:

1. Sends a `UserPrompt` and consumes the complete turn under `turn_timeout`.
   `TextDelta` events are forwarded as `AgentText` while they stream.
2. Rejects protocol violations as `provider_error` **before executing
   anything**: missing or repeated `TurnFinished`, unknown events, reused call
   IDs, or a finish reason that does not match the proposed calls.
3. Validates every proposed call with the catalogue. One invalid call prevents
   the whole round; each call receives an explanation.
4. Builds an `ActionPlan` bound to the current snapshot. The rationale is the
   model's text for that turn (at most 4000 characters), shown to the auditor.
   It emits `AgentPlan`, then calls `ToolGateway.execute()`, which applies the
   policy, the auditor approval and the live-context checks.
5. Returns a `ToolResults` message containing **one result per proposed call**:
   an observation, or a JSON error with `"executed": false` when the call was
   blocked, rejected, invalid, over budget or skipped after a failed operation.
   The model can then adapt its approach rather than retrying blindly.
6. Repeats until the provider completes the turn without calls.

When the gateway blocks for a context reason (changed or unverifiable) and
`context_client` is supplied, the orchestrator reads `target.context` again.
A changed snapshot is registered in the store, announced with an `AgentNotice`,
and used for subsequent plans; it never inherits earlier approvals.

### Budgets

`AgentBudget` bounds each request:

| Limit | Default | When exceeded |
|---|---|---|
| `max_tool_rounds` | 8 | Calls are answered as not executed and the model gets one final turn without tools. |
| `max_tool_calls` | 32 | Same as above, counted over all rounds of the request. |
| `max_calls_per_round` | 8 | The whole round is answered as not executed. |
| `max_output_chars` | 64000 | The provider turn is cancelled. |
| `max_prompt_chars` | 16000 | `submit()` raises `ValueError`. |
| `turn_timeout` | 300 s | The provider turn is cancelled (`timeout`). |

Approval waits are bounded by the gateway's `approval_timeout`, not by
`turn_timeout`.

### Outcomes, cancellation and journal

`submit()` returns a `RunOutcome` (`completed`, `cancelled`,
`budget_exhausted`, `timeout` or `provider_error`) with the rounds, tool calls
and output characters consumed. The sink also receives a final
`AgentRunFinished`. Concurrent requests and requests after `close()` raise
`OrchestratorError`.

`cancel()` interrupts the provider turn, a pending approval or the dispatch in
progress. **Started target operations are not rolled back**: completed results
are reported, and an `AgentNotice` names a call that may still complete on the
target. Task cancellation from another source propagates unchanged.

The provider protocol expects a result for each proposed call. If a request
stops with calls left unanswered (cancellation during approval or dispatch, or
a model still proposing tools after budget exhaustion),
`requires_new_session` becomes true and further requests are refused. A
cancellation while text is streaming keeps the conversation usable.

With a store, the orchestrator records the prompt, the assistant text
(including partial text when interrupted), each proposed call, each result and
an `error` entry for any outcome other than `completed`, all bound to the
snapshot current at the time. This journal records what happened; it is never
replayed as authorization.

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

The [orchestrator](#agent-orchestrator) collects a complete turn before executing proposals. It
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

## GitHub Copilot provider

`pyGdbClient.llm.copilot.CopilotProvider` implements `LlmProvider` using the
optional `github-copilot-sdk` package (imported as `copilot`). Importing the
adapter or running the dashboard does not import the SDK or start a runtime.
The base installation remains usable without Copilot.

### Installation and runtime

This adapter pins [SDK 1.0.27](https://pypi.org/project/github-copilot-sdk/1.0.27/)
and [runtime 1.0.104](https://github.com/github/copilot-cli/releases/tag/v1.0.104).
The published wheel's API and embedded runtime pin were inspected; its Python
minimum is 3.11, compatible with the project's 3.12 minimum. The SDK is still
classified alpha. Upgrading either component requires revalidating the
integration, not just loosening a dependency bound.

Install the extra, then provision the runtime explicitly:

```console
python -m pip install '.[copilot]'
python -m copilot download-runtime
```

The download command prints the cached executable path and verifies the
release's checksums. On an offline workstation, provision the complete
platform-specific bundle beforehand, including its adjacent native library
and assets. Supplying only a copied executable is not enough.

`CopilotConfig.runtime_path` must be an absolute executable path. The provider
never invokes the SDK's automatic downloader: it passes that path explicitly
to a managed stdio connection. `status()` checks the installed SDK version,
runtime startup, the runtime's reported version and authentication. Missing or
incompatible components return `unavailable` with guidance; they do not enable
a degraded or unrestricted session. Transport operations have a finite
`rpc_timeout` (30 seconds by default); inference itself is bounded by the
orchestrator's turn timeout.

### Authentication

Two modes are supported:

- **Existing CLI login** (no explicit token): sign in outside the dashboard
  using `copilot login`, including its browser/device authorization flow.
  The provider sets `use_logged_in_user=True`. Credentials remain managed by
  the CLI; pyGdbClient does not run a shell login command, collect a password,
  open a browser automatically or read the CLI's credential files.
- **Explicit GitHub token**: pass `github_token` from an application-owned
  secret source. It is passed to the SDK in memory, excluded from the
  configuration's representation, and never included in the target snapshot,
  prompts, tool arguments or SQLite journal. This disables logged-in-user
  fallback (`use_logged_in_user=False`).

For example, after explicitly provisioning the runtime:

```python
import os
from pathlib import Path
from pyGdbClient.llm.copilot import CopilotConfig, CopilotProvider

provider = CopilotProvider(
    CopilotConfig(
        runtime_path=Path("/absolute/path/printed/by/download-runtime/copilot"),
        github_token=os.environ["PYGDB_COPILOT_TOKEN"],
        # Omit github_token to use existing CLI login instead.
    )
)
try:
    status = await provider.status()
    # Show status.message; open a session only if authenticated.
finally:
    await provider.close()
```

`status()` does not initiate login or inference and does not echo the SDK's
account name, status message or errors. A negative authentication probe returns
`required` and login guidance; the SDK's boolean probe cannot reliably
distinguish never-authenticated credentials from expired ones. Authentication
is rechecked before each new conversation. A positive status does not
guarantee model entitlement: subscription, token permissions, network access
and organization policy can still prevent inference.

### Session restrictions and tool handoff

Every `open_session()` creates a new backend UUID, separate from the
application-owned session ID. There is no backend resumption or automatic
history replay. The full initial `TargetContext` is included as structured
data in a replacement system message: MCU, revision, architecture, debug
transport, OCD, core, memory map and SVD metadata, including provenance and
unavailability reasons. An SVD path is server metadata, not a local attachment;
the adapter does not read or upload that file.

Both authentication modes enforce the same restrictions:

- Only explicitly declared application tools appear in `available_tools`.
  This release applies that allowlist to the **entire** merged catalogue,
  including custom tools. The empty list would disable custom tools too.
  The adapter maps them to collision-free `pygdb_N` names and maps proposals
  back to their original catalogue names.
- Every declaration has `handler=None`: the SDK cannot execute a target RPC.
  `skip_permission=True` applies only to these inert declarations; target
  consent belongs exclusively to `ToolGateway`. Every SDK permission request
  is rejected, including unexpected custom-tool requests. There is no
  approve-all permission handler.
- Config and instruction discovery, file hooks, host Git operations, skills,
  plugins, custom agents, MCP servers, memory, infinite sessions and session
  store integration are disabled. Plugins and built-in skills are also cleared
  through the mutable session options API; a negative acknowledgement blocks
  session opening. The runtime starts in an empty temporary working directory,
  not the auditor's repository.

With an explicit token, SDK `mode="empty"` also isolates runtime storage in
that temporary directory. CLI login uses `mode="copilot-cli"` because empty
mode deliberately disables the system keychain. The explicit session
restrictions above apply in either mode, rather than relying on mode defaults.

Text deltas are streamed without duplicating the final assistant message.
The complete `assistant.message` supplies the whole proposed action batch,
which is validated before any `ToolCallProposed` is emitted. Partial tool JSON
is not executable. Multi-chunk complete messages are currently rejected rather
than risking approval of an incomplete chain.

The orchestrator sends the resulting plan through the gateway and returns
exactly one observation or error per call. Only then does the adapter answer
the SDK's pending external requests. It verifies the session, request ID, call
ID, name and arguments against the proposal. Requests may arrive together or
sequentially; no target tool handler is installed in either case. Failed
gateway results remain failures, not ordinary user messages or synthetic
successful tool results.

Malformed/reused calls, unsolicited tools, subagent activity, incomplete
streams, queue overflow, transport failures and rejected feedback raise
`ProviderError`, never successful completion. Vendor exception text is not
forwarded, and Python SDK diagnostics emitted within adapter calls have their
message and traceback suppressed to avoid leaking sensitive details.
Cancellation aborts runtime inference; it never rolls back target operations.
An interrupted pending batch requires a new session. `close()` detaches the
session; `provider.close()` also stops the owned runtime and removes its
temporary working directory.

### Validation and confidentiality limits

Client tests use the **installed pinned SDK** (real clients, sessions, events,
permission dispatch and generated RPC serialization) over an offline
transport double. They cover authentication, restriction payloads, complete
and sequential tool batches, error sanitization, cancellation, and a simulated
SDK-to-orchestrator-to-gateway round with auditor approval accepted or refused.
They require no GitHub account, network model call, GDB module or target.
These tests do not certify a live account, model or runtime sandbox.

Opening a real conversation intentionally sends the snapshot, auditor prompts
and approved observations to GitHub Copilot. CLI credentials, runtime-owned
diagnostic/session files and service-side retention remain under their
respective policies; the local SQLite store is not a guarantee of exclusive
local persistence. In particular CLI-login mode retains the CLI's normal data
directory. Do not send sensitive audit data without authorization. Dedicated
long-context and confidentiality controls follow in a later increment.

## Dashboard agent CLI

The dashboard has a second prompt, below the command prompt, dedicated to the
agent. It is enabled by selecting a provider when starting the client:

```console
pyGdbClient ws://192.0.2.15:1234 --llm github-copilot \
    --copilot-runtime /absolute/path/printed/by/download-runtime/copilot
```

Without `--llm`, the prompt stays visible but only explains how to enable it;
the dashboard behaves as before. Agent text, proposed plans, tool
observations, notices and the final outcome of each request are written to the
central **COMMAND OUTPUT** console. Model text and observations are rendered
literally: control and bidirectional-formatting characters are replaced by
`�`, rich markup is not interpreted, and each observation display is limited to
2000 characters (the provider still receives the gateway's bounded result).

### Launcher options

| Option | Purpose |
| ------ | ------- |
| `--llm PROVIDER` | Enable the agent CLI with a registered provider (`github-copilot`). |
| `--llm-store FILE` | Retain conversations and target snapshots in this SQLite file. By default nothing is persisted. |
| `--copilot-runtime PATH` | Copilot runtime executable; defaults to `$PYGDB_COPILOT_RUNTIME`. Relative paths are made absolute; nothing is downloaded. |
| `--copilot-model MODEL` | Model requested from Copilot (default: the runtime's choice). |
| `--copilot-token-env NAME` | Read a GitHub token from environment variable `NAME`. Without it, the `copilot login` account is used. |

A token is never accepted on the command line, where other local users could
read it from the process list. Invalid provider options stop the launcher with
an error before the dashboard starts.

### Agent commands

Lines starting with `/` are handled locally and never sent to the model:

| Command | Effect |
| ------- | ------ |
| *text* | Send a natural-language request. The first one opens a session. |
| `/status` | Show provider readiness and authentication guidance (for example, run `copilot login`). |
| `/new` | Cancel any request, close the session and open one on a fresh target snapshot. |
| `/cancel` | Interrupt the running request; started target operations are not rolled back. |
| `/end` | Cancel any request and close the session. |
| `/help` | List these commands. The dashboard `help` command lists them too. |

### Session opening

`pyGdbClient.llm.controller.AgentController` owns the lifecycle independently
of Textual, so other front ends can reuse it. Opening a session:

1. checks the provider status; a provider that is unavailable, or whose
   authentication is `required` or `expired`, is reported with its guidance
   before any target access;
2. reads a fresh [`target.context`](#target-context) snapshot;
3. opens a provider conversation with that snapshot and the
   [tool catalogue](#tool-catalogue-and-rpc-gateway) definitions;
4. registers the session in the optional SQLite store;
5. binds a new `ToolGateway` using the default `ProtectionPolicy` and the
   dashboard's [auditor approval](#auditor-approval) modal, then an
   `AgentOrchestrator` with the default budgets.

The console then lists the facts transmitted to the provider: MCU, revision,
architecture, debug transport (JTAG or SWD), OCD, core, SVD file and memory
map regions, each with its source or unavailability reason. If any step fails
after the provider conversation was opened, that conversation is closed.

Only one request runs at a time. A conversation left with unanswered tool
calls (see [cancellation](#outcomes-cancellation-and-journal)) refuses further
requests until `/new` is used. Quitting the dashboard cancels any request and
pending approval, then closes the conversation, the provider and the store.

### Adding a provider

`pyGdbClient.llm.registry` holds `ProviderFactory` objects. A factory declares
its own launcher options, prefixed by its name, and builds an `LlmProvider`
from the parsed options and the environment without network access:

```python
class MyProviderFactory:
    provider_id = "my-provider"
    description = "My provider"

    def add_arguments(self, parser):
        parser.add_argument_group("My provider").add_argument("--my-provider-url")

    def create(self, arguments, environ):
        return MyProvider(arguments.my_provider_url)


registry = default_registry()
registry.register(MyProviderFactory())
```

Identifiers must be unique lowercase names, and the built provider must report
the same `provider_id`. A provider only receives conversations: it never gets
the gateway, the RPC client, the approval callback or the store.
