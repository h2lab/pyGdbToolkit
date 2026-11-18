<!--
SPDX-FileCopyrightText: 2026 H2Lab Development Team
SPDX-License-Identifier: Apache-2.0
-->
# Agent-assisted audit

The dashboard can attach an LLM agent that helps with debugging, auditing and
analysis. The auditor states an intention in natural language; the agent plans
calls to the OCD, GDB and toolkit commands exposed by the server, and its
output appears in the central **COMMAND OUTPUT** console. Every proposed call
passes a local protection policy, and risky chains are shown to the auditor
for explicit approval before anything reaches the target.

This page is the user guide. Contracts, trust boundaries and the full option
reference are in [LLM integration](llm.md).

## Quick start

1. Start `pyGdbServer` as described in [Server and client](getting-started.md#server-and-client).
2. Install the optional Copilot support, provision its runtime and sign in:

   ```console
   python -m pip install '.[copilot]'
   python -m copilot download-runtime
   copilot login
   ```

3. Start the client with a provider:

   ```console
   pyGdbClient ws://localhost:1234 --llm github-copilot \
       --copilot-runtime /absolute/path/printed/by/download-runtime/copilot-runtime \
       --llm-store audit.sqlite
   ```

   Alternatively, use a local OpenAI-compatible server (Ollama, llama.cpp,
   vLLM) running on trusted equipment:

   ```console
   python -m pip install '.[openai]'
   pyGdbClient ws://localhost:1234 --llm openai \
       --openai-url http://ollama.lan:11434/v1 --openai-model qwen3 \
       --llm-store audit.sqlite
   ```

   Alternatively, connect to an **already running OpenCode V2 server** that
   has been configured to use a model, such as Ollama on the network:

   ```console
   python -m pip install '.[opencode]'
   export OPENCODE_PASSWORD='<server password>'
   pyGdbClient ws://localhost:1234 --llm opencode \
       --opencode-url http://opencode.lan:4096 \
       --opencode-password-env OPENCODE_PASSWORD
   ```

   OpenCode proxies the requests to its configured model. Its native tools
   are disabled; model-proposed target operations still require the local
   policy and approvals. See [OpenCode setup and limitations](llm.md#opencode-v2-provider),
   including server startup, authentication and the structured response protocol.

4. Type `/status` in the agent prompt (the second prompt, below the command
   prompt) to check readiness and authentication, then type a request.

The **LLM CREDITS** panel, below the OCD log, shows the used and remaining
percentage of each provider quota and is refreshed after every request. When
premium requests are exhausted, the model may stop answering; check this panel
first.

Without `--llm-store`, conversations are kept in memory only and cannot be
resumed after the dashboard closes. See [launcher options](llm.md#launcher-options)
and [agent commands](llm.md#agent-commands) for the complete reference, and
[authentication](llm.md#authentication) for token-based sign-in.

## What the agent receives

The first request opens a session on a fresh snapshot of the target. The
console lists what is transmitted to the provider:

- MCU name and revision, architecture and core;
- debug transport (JTAG or SWD) and OCD name;
- memory map regions;
- the loaded SVD file, when one exists.

Each fact shows its source, or why it is unavailable. The agent then works
through a fixed tool catalogue: target status and cores, SVD peripherals,
memory and register reads, and toolkit, GDB, OCD and MI commands. It never
receives the RPC connection, the approval decision or the local store.

## Approving plans

Each model turn proposes a plan: one or more calls executed as a single chain.
The policy classifies the whole plan:

- **allowed**: metadata reads such as `svd_peripherals` or `target_status` run
  immediately;
- **approval required**: memory and register reads, core selection, and
  commands whose effects cannot be proven (`gdb_command`, `ocd_command`,
  `toolkit_command`, `mi_command`) open an approval dialog;
- **denied**: never dispatched, and cannot be overridden.

The dialog shows the plan, each call's policy reasons, the target context and
the exact RPC invocations. Before accepting, check that:

- the addresses and lengths match the request and stay within expected regions;
- no command writes memory or registers, erases or programs flash or OTP,
  changes option bytes, resets or halts the target unexpectedly;
- execution commands (`continue`, `step`) have a stopping condition, such as a
  breakpoint or watchpoint.

An approval covers one attempt of the exact plan shown. A rejected plan is not
executed; the agent is told it was rejected and can propose an alternative.
`/cancel` interrupts a request, but operations already started on the target
are not rolled back.

## Example workflows

The client test suite replays these workflows against a simulated server in
`tests_client/test_llm_scenarios.py`. No GDB or target is needed.

### Reverse-engineer a driver from peripheral state

> How does the firmware configure USART2?

The agent lists SVD peripherals (allowed) to locate USART2. It then proposes
reading the peripheral's control register together with the core registers,
which needs approval. Next it proposes placing a watchpoint on that register
and continuing execution, which needs a second approval because GDB commands
are opaque. Finally it reads the register again and explains the transition,
for example that the driver enabled the transmitter, the receiver and then the
peripheral.

### Measure execution time

> How long does uart_send take?

The agent proposes one chain: break at `uart_send`, continue, read
`DWT_CYCCNT`, run to the end of the function with `finish`, then read
`DWT_CYCCNT` again. The auditor approves the whole chain once; the calls run
in order and the agent reports the cycle difference.

### Refuse a risky chain

> Check whether the boot path survives an erase.

The agent proposes `flash erase_sector` and `reset halt` OCD commands. The
dialog flags them as opaque commands that may modify flash or OTP. If the
auditor rejects them, nothing is sent to the target; the agent is told that
each call was rejected and can propose a read-only alternative.

### Resume an investigation after a restart

With `--llm-store`, close the dashboard, start it again, then:

```text
/sessions
/resume ID
```

where `ID` is an identifier listed by `/sessions`.

The new conversation starts on a fresh target snapshot, seeded with a bounded
recollection of the previous one. The recollection is context, not authority:
earlier approvals are not inherited, and every new plan is checked again.
Long conversations continue the same way automatically when
`--llm-context-limit` is reached; `/compact` triggers it manually.

## Confidentiality

By default, recognizable textual secrets (keys, tokens, credential
assignments) are masked before disclosure, but raw memory and register values
are sent as read.

To give the agent complete visibility of a **test** target, including symbols,
dumps and test cryptographic material, use
[`--llm-test-data`](llm.md#test-data-analysis-mode). Each session then shows a
disclosure warning; the policy and approvals still apply.

GitHub Copilot is an external service. **For production data, use a local model
running on trusted equipment instead of an external provider.** The
[OpenAI-compatible provider](llm.md#openai-compatible-provider) can attach a
LAN inference server such as Ollama or llama.cpp; verify what that server logs
and retains before sending production data. Additional providers can be added
through the [provider registry](llm.md#adding-a-provider). Protect the
`--llm-store` file: it holds unmasked prompts, snapshots and observations.

## Troubleshooting

| Symptom | Action |
| ------- | ------ |
| The agent prompt only explains how to enable it | Start the client with `--llm github-copilot`, `--llm openai` or `--llm opencode`. |
| `/status` reports the provider unavailable | Copilot: install the `copilot` extra, run `python -m copilot download-runtime` and pass the printed path with `--copilot-runtime` or `PYGDB_COPILOT_RUNTIME`. OpenAI-compatible: install the `openai` extra and check `--openai-url` and the server. OpenCode: install the `opencode` extra and check that the V2 server is running at `--opencode-url`. |
| `/status` reports authentication required | Copilot: run `copilot login`, or use `--copilot-token-env NAME`. OpenAI-compatible: use `--openai-key-env NAME`. OpenCode: set `--opencode-password-env NAME` to the variable holding the server password. |
| OpenAI-compatible completion returns HTTP 404 | Check both the `/v1` base URL and exact `--openai-model` name. Ollama returns 404 for an uninstalled model too; check `ollama list` on the server. |
| The model does not support tool calling | Select an installed tool-capable model, for example `--openai-model llama3.2:latest` on Ollama. `qwen2:0.5b-instruct` supports plain chat, not this agent's tools. |
| OpenCode reports an invalid structured response | Select a model configured on OpenCode that reliably follows the JSON protocol; the adapter rejects unstructured answers rather than bypassing the local tool gateway. |
| Requests are refused after `/cancel` | The conversation has unanswered tool calls; use `/new`. |
| `/sessions` is empty after a restart | Use the same `--llm-store` file in each run. |
| The agent lost earlier details | Continuation keeps a bounded recollection; ask the agent to observe the target again. |
