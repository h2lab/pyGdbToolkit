# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Simulated audit workflows driven end to end through the dashboard agent CLI.

Each scenario runs the real dashboard, controller, orchestrator, policy, gateway,
approval modal and store against a loopback pyGdbServer double and a scripted
model. No GDB module, network model or target is involved.
"""

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

import pytest

from pyGdbClient.llm.contracts import (
    ContextFact,
    MemoryMapContext,
    MemoryRegion,
    TargetContext,
    ToolCall,
    ToolDefinition,
)
from pyGdbClient.llm.controller import AgentSettings
from pyGdbClient.llm.provider import (
    LlmSession,
    ProviderEvent,
    TextDelta,
    ToolCallProposed,
    ToolResults,
    TurnFinished,
    UserPrompt,
)
from pyGdbClient.llm.recollection import RECOLLECTION_HEADER
from pyGdbClient.llm.scripted import ScriptedProvider, ScriptedSession, ScriptedTurn
from pyGdbClient.llm.store import ContextStore, EntryKind
from agent_harness import AgentApp, decide, output, type_agent, until
from rpc_fakes import FakeRpcServer

_USART2_CR1 = 0x40004400
_DWT_CYCCNT = 0xE0001004


def _context() -> TargetContext:
    return TargetContext(
        "ctx-u585",
        ContextFact("STM32U585", "lscpu"),
        ContextFact("rev X", "DBGMCU_IDCODE"),
        ContextFact("armv8-m.main", "gdb"),
        ContextFact("jtag", "ocd"),
        ContextFact("openocd 0.12.0", "server"),
        ContextFact("cortex-m33 core 0", "target.core"),
        MemoryMapContext(
            (
                MemoryRegion(0x08000000, 0x08200000, "flash", "svd", "FLASH", "rx"),
                MemoryRegion(0x20000000, 0x200C0000, "ram", "svd", "SRAM", "rwx"),
                MemoryRegion(0x40000000, 0x50000000, "peripheral", "svd", "APB", "rw"),
            ),
            "svd",
        ),
        ContextFact("STM32U585.svd", "configuration"),
    )


def _call(call_id: str, name: str, **arguments: Any) -> ToolCall:
    return ToolCall(call_id, name, arguments)


def _plan(*calls: ToolCall, text: str = "") -> ScriptedTurn:
    events: list[ProviderEvent] = [TextDelta(text)] if text else []
    events.extend(ToolCallProposed(call) for call in calls)
    return (*events, TurnFinished("tool_calls"))


def _answer(text: str) -> ScriptedTurn:
    return (TextDelta(text), TurnFinished())


def _done(*lines: str) -> dict[str, Any]:
    return {"class": "done", "record": "done", "output": list(lines)}


def _read(address: int, length: int) -> str:
    return f"-data-read-memory-bytes 0x{address:x} {length}"


class RecordingProvider(ScriptedProvider):
    """Scripted model keeping the sessions it opened, to inspect what it received."""

    def __init__(self, *turns: ScriptedTurn) -> None:
        super().__init__(turns)
        self.sessions: list[ScriptedSession] = []

    async def open_session(
        self, session_id: str, context: TargetContext, tools: tuple[ToolDefinition, ...]
    ) -> LlmSession:
        session = await super().open_session(session_id, context, tools)
        assert isinstance(session, ScriptedSession)
        self.sessions.append(session)
        return session


class Target:
    """pyGdbServer double whose memory and commands evolve like a running driver."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.memory: dict[int, list[str]] = {}

    def responses(self) -> dict[str, Any]:
        return {
            "target.context": json.loads(json.dumps(asdict(_context()))),
            "target.status": {"state": "stopped", "thread": 1},
            "svd.peripherals": {
                "svd": "STM32U585.svd",
                "peripherals": [{"name": "USART2", "base": _USART2_CR1}],
            },
            "mi.execute": self._mi,
            "command.execute": self._command,
        }

    def _mi(self, params: dict[str, Any]) -> dict[str, Any]:
        command = str(params["command"])
        self.commands.append(command)
        for address, values in self.memory.items():
            if command.startswith(f"-data-read-memory-bytes 0x{address:x} "):
                return _done(f"memory=[{{begin=0x{address:x},contents={values.pop(0)}}}]")
        return _done('register-values=[{number="15",value="0x08000412"}]')

    def _command(self, params: dict[str, Any]) -> dict[str, Any]:
        command = str(params["command"])
        self.commands.append(command)
        replies = {
            "gdb watch *(unsigned int *)0x40004400": "Hardware watchpoint 1",
            "gdb continue": "Old value = 0  New value = 13  usart_init () at usart.c:42",
            "gdb break uart_send": "Breakpoint 2 at 0x08000530",
            "gdb finish": "Run till exit from uart_send ()",
        }
        return _done(replies.get(command, "ok"))


def _results(session: ScriptedSession, index: int) -> list[dict[str, Any]]:
    message = session.inputs[index]
    assert isinstance(message, ToolResults)
    return [json.loads(result.content) for result in message.results]


def test_driver_reverse_engineering_by_peripheral_state_tracking(tmp_path: Path) -> None:
    """The agent locates a peripheral, watches its register and explains the driver."""

    async def exercise() -> None:
        target = Target()
        target.memory[_USART2_CR1] = ["00000000", "0d000000"]
        provider = RecordingProvider(
            _plan(_call("p1", "svd_peripherals"), text="Locating USART2\n"),
            _plan(
                _call("m1", "memory_read", address=_USART2_CR1, length=4),
                _call("g1", "register_values"),
            ),
            _plan(
                _call("w1", "gdb_command", command="watch *(unsigned int *)0x40004400"),
                _call("c1", "gdb_command", command="continue"),
            ),
            _plan(_call("m2", "memory_read", address=_USART2_CR1, length=4)),
            _answer("usart_init (usart.c:42) sets CR1 to 0x0D: UE, RE and TE enabled."),
        )
        store = tmp_path / "audit.sqlite3"
        async with FakeRpcServer(target.responses()) as server:
            app = AgentApp(server.url, AgentSettings(provider, store_path=store))
            async with app.run_test(size=(120, 50)) as pilot:
                await until(pilot, lambda: app._connected)
                await type_agent(pilot, app, "How does the firmware configure USART2?")
                first = await decide(pilot, app, approve=True)
                assert "svd.peripherals" in server.methods
                assert _read(_USART2_CR1, 4) in first
                assert "-data-list-register-values x" in first
                watch = await decide(pilot, app, approve=True)
                assert "gdb continue" in watch and "opaque command" in watch
                await decide(pilot, app, approve=True)
                await until(pilot, lambda: "Agent request completed" in output(app))
                console = output(app)
                for expected in (
                    "MCU: STM32U585 (lscpu)",
                    "Debug transport: jtag (ocd)",
                    "SVD: STM32U585.svd (configuration)",
                    "0x40000000-0x4FFFFFFF peripheral rw APB",
                    "Locating USART2",
                    "UE, RE and TE enabled",
                ):
                    assert expected in console
        session = provider.sessions[0]
        assert session.context == _context()
        assert isinstance(session.inputs[0], UserPrompt)
        assert target.commands == [
            _read(_USART2_CR1, 4),
            "-data-list-register-values x",
            "gdb watch *(unsigned int *)0x40004400",
            "gdb continue",
            _read(_USART2_CR1, 4),
        ]
        assert "New value = 13" in json.dumps(_results(session, 3))
        assert "0d000000" in json.dumps(_results(session, 4))
        with ContextStore(store) as journal:
            kinds = [entry.kind for entry in journal.history(session.session_id)]
        assert kinds.count(EntryKind.TOOL_CALL) == 6
        assert kinds.count(EntryKind.TOOL_RESULT) == 6
        assert kinds[-1] is EntryKind.ASSISTANT

    asyncio.run(exercise())


def test_cycle_measurement_runs_as_one_approved_chain() -> None:
    """A timing measurement chain is approved once, then executed in order."""

    async def exercise() -> None:
        target = Target()
        target.memory[_DWT_CYCCNT] = ["10270000", "a0860100"]
        chain = (
            _call("b1", "gdb_command", command="break uart_send"),
            _call("c1", "gdb_command", command="continue"),
            _call("t1", "memory_read", address=_DWT_CYCCNT, length=4),
            _call("f1", "gdb_command", command="finish"),
            _call("t2", "memory_read", address=_DWT_CYCCNT, length=4),
        )
        provider = RecordingProvider(
            _plan(*chain, text="Measuring uart_send with DWT_CYCCNT\n"),
            _answer("uart_send took 90000 cycles."),
        )
        async with FakeRpcServer(target.responses()) as server:
            app = AgentApp(server.url, AgentSettings(provider))
            async with app.run_test(size=(120, 50)) as pilot:
                await until(pilot, lambda: app._connected)
                await type_agent(pilot, app, "How long does uart_send take?")
                details = await decide(pilot, app, approve=True)
                for call in chain:
                    assert call.call_id in details
                await until(pilot, lambda: "Agent request completed" in output(app))
                assert "uart_send took 90000 cycles." in output(app)
        assert target.commands == [
            "gdb break uart_send",
            "gdb continue",
            _read(_DWT_CYCCNT, 4),
            "gdb finish",
            _read(_DWT_CYCCNT, 4),
        ]
        results = json.dumps(_results(provider.sessions[0], 1))
        assert results.index("10270000") < results.index("a0860100")

    asyncio.run(exercise())


def test_rejected_risky_chain_is_never_dispatched_and_explained_to_the_model() -> None:
    """Flash erase and reset are shown to the auditor; refusal dispatches nothing."""

    async def exercise() -> None:
        target = Target()
        provider = RecordingProvider(
            _plan(
                _call("e1", "ocd_command", command="flash erase_sector 0 0 last"),
                _call("r1", "ocd_command", command="reset halt"),
                text="Erasing the first flash bank to test the boot path\n",
            ),
            _answer("Understood; the flash is left untouched."),
        )
        async with FakeRpcServer(target.responses()) as server:
            app = AgentApp(server.url, AgentSettings(provider))
            async with app.run_test(size=(120, 50)) as pilot:
                await until(pilot, lambda: app._connected)
                await type_agent(pilot, app, "Check whether the boot path survives an erase")
                details = await decide(pilot, app, approve=False)
                assert "monitor flash erase_sector 0 0 last" in details
                assert "monitor reset halt" in details
                assert "modify flash/OTP" in details
                await until(pilot, lambda: "Agent request completed" in output(app))
                assert "the flash is left untouched" in output(app)
            assert "command.execute" not in server.methods
        assert target.commands == []
        refusals = _results(provider.sessions[0], 1)
        assert len(refusals) == 2
        assert all("rejected" in json.dumps(refusal) for refusal in refusals)

    asyncio.run(exercise())


@pytest.mark.parametrize("approve", [True, False])
def test_investigation_resumes_after_restart_without_inheriting_approval(
    tmp_path: Path, approve: bool
) -> None:
    """A journaled investigation continues after restart; every read is approved again."""

    async def exercise() -> None:
        store = tmp_path / "audit.sqlite3"
        target = Target()
        target.memory[0x46020C00] = ["00000500"]
        before = RecordingProvider(_answer("Noted: the clock tree uses HSI16."))
        async with FakeRpcServer(target.responses()) as server:
            app = AgentApp(server.url, AgentSettings(before, store_path=store))
            async with app.run_test(size=(120, 50)) as pilot:
                await until(pilot, lambda: app._connected)
                await type_agent(pilot, app, "Remember: SYSCLK should come from HSI16")
                await until(pilot, lambda: "Agent request completed" in output(app))
        earlier = before.sessions[0].session_id

        after = RecordingProvider(
            _plan(_call("m1", "memory_read", address=0x46020C00, length=4)),
            _answer("RCC_CR confirms HSI16 is on."),
        )
        async with FakeRpcServer(target.responses()) as server:
            app = AgentApp(server.url, AgentSettings(after, store_path=store))
            async with app.run_test(size=(120, 50)) as pilot:
                await until(pilot, lambda: app._connected)
                await type_agent(pilot, app, "/sessions")
                await until(pilot, lambda: earlier in output(app))
                await type_agent(pilot, app, f"/resume {earlier}")
                await until(pilot, lambda: f"Continues {earlier}:" in output(app))
                await type_agent(pilot, app, "Check RCC_CR")
                details = await decide(pilot, app, approve=approve)
                assert _read(0x46020C00, 4) in details
                await until(pilot, lambda: "Agent request completed" in output(app))
            assert ("mi.execute" in server.methods) is approve
        prompt = after.sessions[0].inputs[0]
        assert isinstance(prompt, UserPrompt)
        assert prompt.text.startswith(RECOLLECTION_HEADER)
        assert "Auditor: Remember: SYSCLK should come from HSI16" in prompt.text
        assert prompt.text.endswith("Current auditor request:\nCheck RCC_CR")
        assert ("00000500" in json.dumps(_results(after.sessions[0], 1))) is approve

    asyncio.run(exercise())
