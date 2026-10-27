# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Headless auditor interactions use neither GDB nor connected equipment."""

import asyncio
from dataclasses import asdict
import json

import pytest
from textual.widgets import Button, Static

from pyGdbClient.app import PyGdbClientApp
from pyGdbClient.llm.approval import ApprovalRequest
from pyGdbClient.llm.approval_screen import ApprovalScreen
from pyGdbClient.llm.contracts import (
    ActionPlan,
    ContextFact,
    MemoryMapContext,
    PlannedAction,
    TargetContext,
    ToolCall,
)
from pyGdbClient.llm.gateway import GatewayError, ToolGateway
from pyGdbClient.llm.policy import ProtectionPolicy
from rpc_fakes import FakeRpcServer


class OfflineApp(PyGdbClientApp):
    async def _connect_and_initialize(self) -> None:
        """Mount the real dashboard without starting any target RPC."""


def _request() -> ApprovalRequest:
    return ApprovalRequest(
        "request-1",
        "abc123",
        json.dumps(
            {
                "plan": {
                    "steps": [{"command": "monitor reset halt"}],
                    "rationale": "[bold]literal[/]",
                },
                "policy": {"reasons": ["May reset target"]},
                "context": {"mcu": "Test MCU", "context_id": "ctx-1"},
                "rpc_calls": [
                    {"method": "command.execute", "params": {"command": "monitor reset halt"}}
                ],
            }
        ),
    )


@pytest.mark.parametrize("action", ["escape", "enter", "reject", "accept"])
def test_explicit_consent_and_safe_keyboard_default(action: str) -> None:
    async def exercise() -> None:
        app = OfflineApp("ws://127.0.0.1:1")
        async with app.run_test(size=(110, 45)) as pilot:
            task = asyncio.create_task(app.request_plan_approval(_request()))
            await pilot.pause()
            assert isinstance(app.screen, ApprovalScreen)
            assert app.focused is app.screen.query_one("#approval-reject", Button)
            details = app.screen.query_one("#approval-details", Static)
            text = str(details.render())
            for expected in [
                "monitor reset halt",
                "[bold]literal[/]",
                "Test MCU",
                "May reset target",
            ]:
                assert expected in text
            assert text.index("COMPLETE ORDERED PLAN") < text.index("TARGET CONTEXT")
            if action in ("escape", "enter"):
                await pilot.press(action)
            else:
                await pilot.click(f"#approval-{'accept' if action == 'accept' else 'reject'}")
            response = await asyncio.wait_for(task, 1)
            assert response.request_id == "request-1"
            assert response.digest == "abc123"
            assert response.approved is (action == "accept")
            await pilot.pause()
            assert not isinstance(app.screen, ApprovalScreen)
            assert app._approval_screen is None

    asyncio.run(exercise())


def test_cancellation_removes_modal_and_late_click_cannot_approve() -> None:
    async def exercise() -> None:
        app = OfflineApp("ws://127.0.0.1:1")
        async with app.run_test(size=(110, 45)) as pilot:
            task = asyncio.create_task(app.request_plan_approval(_request()))
            await pilot.pause()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await pilot.pause()
            assert not isinstance(app.screen, ApprovalScreen)
            assert app._approval_screen is None
            next_task = asyncio.create_task(app.request_plan_approval(_request()))
            await pilot.pause()
            await pilot.press("escape")
            assert not (await next_task).approved

    asyncio.run(exercise())


def test_overlapping_dashboard_approvals_fail_explicitly() -> None:
    async def exercise() -> None:
        app = OfflineApp("ws://127.0.0.1:1")
        async with app.run_test(size=(110, 45)) as pilot:
            task = asyncio.create_task(app.request_plan_approval(_request()))
            await pilot.pause()
            with pytest.raises(RuntimeError, match="already pending"):
                await app.request_plan_approval(_request())
            await pilot.press("escape")
            assert not (await task).approved

    asyncio.run(exercise())


def test_expiration_closes_modal_without_consent() -> None:
    async def exercise() -> None:
        app = OfflineApp("ws://127.0.0.1:1")
        async with app.run_test(size=(110, 45)) as pilot:
            task = asyncio.create_task(app.request_plan_approval(_request()))
            await pilot.pause()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(task, 0.01)
            await pilot.pause()
            assert not isinstance(app.screen, ApprovalScreen)
            assert app._approval_future is None

    asyncio.run(exercise())


def test_cancellation_during_mount_does_not_leave_an_orphan_dialog() -> None:
    async def exercise() -> None:
        app = OfflineApp("ws://127.0.0.1:1")
        async with app.run_test(size=(110, 45)) as pilot:
            task = asyncio.create_task(app.request_plan_approval(_request()))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await pilot.pause()
            assert not isinstance(app.screen, ApprovalScreen)
            assert app._approval_screen is None

    asyncio.run(exercise())


def test_dashboard_shutdown_cancels_pending_approval() -> None:
    async def exercise() -> None:
        app = OfflineApp("ws://127.0.0.1:1")
        async with app.run_test(size=(110, 45)) as pilot:
            task = asyncio.create_task(app.request_plan_approval(_request()))
            await pilot.pause()
            app.exit()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert app._approval_future is None
        assert app._approval_screen is None

    asyncio.run(exercise())


@pytest.mark.parametrize("approved", [True, False])
@pytest.mark.parametrize("size", [(110, 45), (80, 24)])
def test_gateway_waits_for_real_dashboard_consent(approved: bool, size: tuple[int, int]) -> None:
    """The real callback gates real loopback RPC, including in an ordinary 80-column terminal."""

    async def exercise() -> None:
        fact = ContextFact("test", "configuration")
        context = TargetContext(
            "ctx-1", fact, fact, fact, fact, fact, fact, MemoryMapContext((), "test"), fact
        )
        plan = ActionPlan(
            "plan-1",
            "ctx-1",
            "Inspect MMIO",
            (
                PlannedAction(
                    ToolCall("read", "memory_read", {"address": 0x40000000, "length": 4}),
                    "Inspect peripheral",
                ),
            ),
        )
        async with FakeRpcServer(
            {
                "target.context": json.loads(json.dumps(asdict(context))),
                "mi.execute": {"class": "done", "record": "done", "output": ["observed"]},
            }
        ) as server:
            app = OfflineApp(server.url)
            async with app.run_test(size=size) as pilot:
                await app.client.connect()
                gateway = ToolGateway(
                    app.client, ProtectionPolicy(), approver=app.request_plan_approval
                )
                task = asyncio.create_task(gateway.execute(plan, context))
                await pilot.pause()
                assert isinstance(app.screen, ApprovalScreen)
                assert server.requests == []
                if approved:
                    await pilot.click("#approval-accept")
                    results = await asyncio.wait_for(task, 1)
                    assert len(results) == 1
                    assert not results[0].is_error
                    assert server.requests[-1] == (
                        "mi.execute",
                        {"command": "-data-read-memory-bytes 0x40000000 4", "timeout": 30},
                    )
                else:
                    await pilot.press("escape")
                    with pytest.raises(GatewayError, match="rejected"):
                        await task
                    assert server.requests == []
                await pilot.pause()
                assert not isinstance(app.screen, ApprovalScreen)

    asyncio.run(exercise())
