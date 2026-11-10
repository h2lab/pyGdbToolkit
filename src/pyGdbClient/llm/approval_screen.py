# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Auditor-only Textual modal; model-provided text is displayed without markup."""

import json

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from .approval import ApprovalRequest, ApprovalResponse


class ApprovalScreen(ModalScreen[ApprovalResponse | None]):
    """Review the entire immutable chain; Escape and the initial focus reject it."""

    DEFAULT_CSS = """
    ApprovalScreen {
        align: center middle;
        background: $background 70%;
    }
    ApprovalScreen #approval-dialog {
        width: 90%;
        height: 90%;
        border: thick $warning;
        padding: 1 2;
    }
    ApprovalScreen #approval-title {
        height: auto;
        text-style: bold;
    }
    ApprovalScreen VerticalScroll {
        height: 1fr;
        margin: 1 0;
    }
    ApprovalScreen #approval-details {
        height: auto;
    }
    ApprovalScreen #approval-buttons {
        height: 3;
        align-horizontal: right;
    }
    ApprovalScreen Button {
        margin-left: 1;
    }
    """

    BINDINGS = [("escape", "reject", "Reject plan")]

    def __init__(self, request: ApprovalRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        """Show exact ordered actions, arguments, effects, target facts and local risks."""
        payload = json.loads(self.request.payload_json)
        details = f"Request: {self.request.request_id}\nSHA-256: {self.request.digest}\n\n"
        details += "\n\n".join(
            f"{title}\n{json.dumps(payload[key], indent=2, ensure_ascii=True)}"
            for key, title in (
                ("plan", "COMPLETE ORDERED PLAN"),
                ("policy", "LOCAL POLICY AND RISKS"),
                ("context", "TARGET CONTEXT AND PROVENANCE"),
                ("rpc_calls", "EXACT RPC INVOCATIONS"),
            )
        )
        with Vertical(id="approval-dialog"):
            yield Static(
                "AUDITOR APPROVAL - ENTIRE ACTION CHAIN\n"
                "Target reads can have side effects. Opaque commands may alter the target "
                "or host. There is no rollback.\n"
                "Approval is for one attempt only, subject to live context and policy checks. "
                "Requests expire automatically.",
                id="approval-title",
                markup=False,
            )
            with VerticalScroll():
                yield Static(
                    details,
                    id="approval-details",
                    markup=False,
                )
            with Horizontal(id="approval-buttons"):
                yield Button("Reject", id="approval-reject", variant="default")
                yield Button("Approve entire chain", id="approval-accept", variant="warning")

    def on_mount(self) -> None:
        """Focus the non-authorizing action rather than making Enter approve."""
        self.query_one("#approval-reject", Button).focus()

    def action_reject(self) -> None:
        """Reject without dispatching any part of the chain."""
        self.dismiss(self.request.respond(approved=False))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Return consent only for an explicit click on the approval button."""
        event.stop()
        self.dismiss(self.request.respond(approved=event.button.id == "approval-accept"))
