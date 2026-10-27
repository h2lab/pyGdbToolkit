# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Drive the dashboard agent CLI over a loopback server, without target polling."""

from __future__ import annotations

from collections.abc import Callable

from textual.pilot import Pilot
from textual.widgets import Input, RichLog

from pyGdbClient.app import PyGdbClientApp
from pyGdbClient.llm.approval_screen import ApprovalScreen
from pyGdbClient.llm.controller import AgentSettings


class AgentApp(PyGdbClientApp):
    """Dashboard connecting its transport without the periodic target refresh."""

    def __init__(self, url: str, agent: AgentSettings | None, *, connect: bool = True) -> None:
        super().__init__(url, agent)
        self.connect = connect

    async def _connect_and_initialize(self) -> None:
        """Connect the transport without the dashboard's target polling."""
        if self.connect:
            await self.client.connect()
            self._connected = True


def output(app: PyGdbClientApp) -> str:
    """Return the central console text."""
    return "\n".join(str(line) for line in app.query_one("#command-output", RichLog).lines)


async def type_agent(pilot: Pilot[None], app: PyGdbClientApp, text: str) -> None:
    """Submit one line in the agent prompt."""
    field = app.query_one("#agent-command", Input)
    field.focus()
    field.value = text
    await pilot.press("enter")


async def until(pilot: Pilot[None], condition: Callable[[], bool]) -> None:
    """Wait for a dashboard condition, failing after two seconds."""
    for _ in range(200):
        await pilot.pause(0.01)
        if condition():
            return
    raise AssertionError("condition not reached")


async def decide(pilot: Pilot[None], app: PyGdbClientApp, *, approve: bool) -> str:
    """Wait for the approval modal, return its details and answer it by clicking."""
    await until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
    screen = app.screen
    details = str(screen.query_one("#approval-details").render())
    await pilot.click("#approval-accept" if approve else "#approval-reject")
    # A following plan may push its own modal before this one is observed closed.
    await until(pilot, lambda: app.screen is not screen)
    return details
