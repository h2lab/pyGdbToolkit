# SPDX-FileCopyrightText: 2026 H2Lab Development Team
# SPDX-License-Identifier: Apache-2.0

"""Textual dashboard for a remote pyGdbServer session."""

from __future__ import annotations

import asyncio
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.events import Key, Resize
from textual.widgets import Footer, Header, Input, RichLog, Static, Tree

from .rpc import JsonRpcClient, RpcError

_COMMAND_HISTORY_LIMIT = 40


class PyGdbClientApp(App[None]):
    """Interactive Rich dashboard and command line for pyGdbServer."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #panels {
        height: 1fr;
        width: 100%;
    }
    #logs-column {
        width: 27%;
        min-width: 26;
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    #output-column {
        width: 46%;
        min-width: 34;
        height: 1fr;
        border: round $secondary;
        padding: 0 1;
    }
    #target-column {
        width: 27%;
        min-width: 28;
        height: 1fr;
        border: round $success;
        padding: 0 1;
    }
    .panel-title {
        height: 1;
        text-style: bold;
        color: $accent;
    }
    #gdb-log, #ocd-log {
        height: 1fr;
        border: tall $surface;
        scrollbar-size-vertical: 1;
    }
    #target-summary {
        height: 13;
        border: tall $surface;
    }
    #target-state {
        height: 4;
        padding: 0 1;
        color: $text;
    }
    #svd-title {
        height: 1;
        text-style: bold;
        color: $accent;
    }
    #svd-tree {
        height: 1fr;
        border: tall $surface;
    }
    #command {
        dock: bottom;
        height: 3;
        margin: 0 1;
    }
    Footer {
        dock: bottom;
    }
    Screen.compact #panels {
        layout: vertical;
        overflow-y: auto;
    }
    Screen.compact #logs-column, Screen.compact #output-column, Screen.compact #target-column {
        width: 100%;
        min-width: 0;
        height: 1fr;
    }
    """

    BINDINGS = [("ctrl+q", "quit", "Quit")]

    def __init__(self, url: str) -> None:
        super().__init__()
        self.client = JsonRpcClient(url)
        self._connected = False
        self._ocd_executable = "unknown"
        self._target_refresh_lock = asyncio.Lock()
        self._command_history: list[str] = []
        self._history_position: int | None = None
        self._history_draft = ""

    def compose(self) -> ComposeResult:
        """Build the log, output, target, tree, and command-input regions."""
        yield Header(show_clock=True)
        with Horizontal(id="panels"):
            with Vertical(id="logs-column"):
                yield Static("GDB LOG", classes="panel-title")
                yield RichLog(id="gdb-log", wrap=True, markup=False, highlight=False)
                yield Static("OCD LOG", classes="panel-title")
                yield RichLog(id="ocd-log", wrap=True, markup=False, highlight=False)
            with Vertical(id="output-column"):
                yield Static("COMMAND OUTPUT", classes="panel-title")
                yield RichLog(id="command-output", wrap=True, markup=False, highlight=False)
            with Vertical(id="target-column"):
                yield Static("CONNECTED TARGET", classes="panel-title")
                yield RichLog(id="target-summary", wrap=True, markup=False, highlight=False)
                yield Static("Connecting…", id="target-state")
                yield Static("SVD PERIPHERALS", id="svd-title")
                yield Tree("SVD not loaded", id="svd-tree")
        yield Input(
            placeholder="Toolkit command | gdb <commande> | monitor <commande>",
            id="command",
        )
        yield Footer()

    async def on_mount(self) -> None:
        """Connect, subscribe to logs, and initialize the target dashboard."""
        self.query_one("#command", Input).focus()
        self.run_worker(self._connect_and_initialize(), group="connection")

    def on_resize(self, event: Resize) -> None:
        """Stack dashboard panels when the terminal is too narrow for columns."""
        if event.size.width < 92:
            self.add_class("compact")
        else:
            self.remove_class("compact")

    def on_key(self, event: Key) -> None:
        """Use Up/Down for command history while the CLI input is focused."""
        if event.key not in {"up", "down"}:
            return
        command_input = self.query_one("#command", Input)
        if self.focused is not command_input:
            return
        event.stop()
        self._navigate_command_history(command_input, -1 if event.key == "up" else 1)

    async def _connect_and_initialize(self) -> None:
        try:
            await self.client.connect()
            self._connected = True
            await self.client.request("logs.subscribe")
            self.run_worker(self._consume_notifications(), group="notifications")
            status = await self.client.request("server.status")
            self._ocd_executable = status.get("ocd", {}).get("executable", "unknown")
            self._append_output(
                f"Connected to {self.client.url} · GDB {status['gdb']['interpreter']} · "
                f"OCD port {status['ocd']['gdb_port']}"
            )
            await self._refresh_target_status()
            self.set_interval(2.0, self._refresh_target_status)
            await self.execute_command("lscpu", show_command=False)
            self.run_worker(self._load_svd(), group="svd", exclusive=True)
        except Exception as error:
            self._append_output(f"Connection failed: {error}", error=True)
            self.query_one("#target-state", Static).update("Disconnected")

    async def _consume_notifications(self) -> None:
        while True:
            notification = await self.client.notifications.get()
            if notification.get("method") != "log.event":
                continue
            event = notification.get("params", {})
            source = event.get("source")
            stream = event.get("stream", "")
            message = str(event.get("message", ""))
            if source == "gdb":
                self._append_log("#gdb-log", f"{stream:>6}  {message}")
                if stream == "console":
                    self._append_rendered_output([message])
            elif source == "ocd":
                self._append_log("#ocd-log", f"{stream:>6}  {message}")
            elif source == "server":
                self._append_output(f"[server] {message}", error=stream == "error")

    def _append_log(self, selector: str, message: str) -> None:
        log = self.query_one(selector, RichLog)
        log.write(Text.from_ansi(message))

    def _append_output(self, message: str, *, error: bool = False) -> None:
        log = self.query_one("#command-output", RichLog)
        heading = Text(message, style="bold red" if error else "bold cyan")
        log.write(heading)

    def _append_rendered_output(self, output: list[str]) -> None:
        log = self.query_one("#command-output", RichLog)
        if not output:
            return
        for part in output:
            log.write(Text.from_ansi(part))

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Submit the entered command to the server in a background worker."""
        command = event.value.strip()
        event.input.value = ""
        if not command:
            return
        self._remember_command(command)
        self._history_position = None
        self._history_draft = ""
        normalized = " ".join(command.lower().split())
        if normalized == "help":
            self.show_help()
        elif normalized == "history":
            self.show_command_history()
        elif normalized == "quit":
            self.run_worker(self.quit_client(), group="shutdown")
        elif normalized == "quit --all":
            self.run_worker(self.quit_client(stop_server=True), group="shutdown")
        else:
            self.run_worker(self.execute_command(command), group="commands")

    def _remember_command(self, command: str) -> None:
        self._command_history.append(command)
        del self._command_history[:-_COMMAND_HISTORY_LIMIT]

    def _navigate_command_history(self, command_input: Input, direction: int) -> None:
        if not self._command_history:
            return
        if self._history_position is None:
            if direction > 0:
                return
            self._history_draft = command_input.value
            self._history_position = len(self._command_history)
        next_position = max(0, min(len(self._command_history), self._history_position + direction))
        self._history_position = next_position
        command_input.value = (
            self._history_draft
            if next_position == len(self._command_history)
            else self._command_history[next_position]
        )
        command_input.cursor_position = len(command_input.value)

    def show_command_history(self) -> None:
        """List the retained commands in submission order in the output panel."""
        self._append_output(
            f"Command history ({len(self._command_history)} / {_COMMAND_HISTORY_LIMIT})"
        )
        for index, command in enumerate(self._command_history, start=1):
            self._append_output(f"{index:>2}  {command}")

    def show_help(self) -> None:
        """Display client, toolkit, GDB, and selected OCD command guidance."""
        for paragraph in _help_text(self._ocd_executable):
            self.query_one("#command-output", RichLog).write(paragraph)

    async def quit_client(self, *, stop_server: bool = False) -> None:
        """Quit this dashboard, optionally asking the server to stop its processes."""
        if stop_server and self._connected:
            try:
                await self.client.request("server.shutdown", timeout=15)
            except (RpcError, ConnectionError, TimeoutError) as error:
                self._append_output(f"Server shutdown request failed: {error}", error=True)
        await self.client.close()
        self.exit()

    async def execute_command(self, command: str, *, show_command: bool = True) -> None:
        """Send a natural GDB/toolkit/OCD command to the server."""
        if not self._connected:
            self._append_output("Not connected to pyGdbServer", error=True)
            return
        if show_command:
            self._append_output(f"› {command}")
        try:
            response = await self.client.request(
                "command.execute", {"command": command}, timeout=300
            )
            output = response.get("output", [])
            self._append_rendered_output(output)
            if command.strip().lower() == "lscpu":
                summary = self.query_one("#target-summary", RichLog)
                summary.clear()
                for part in output:
                    summary.write(Text.from_ansi(part))
            if response.get("class") == "error":
                self._append_output(response.get("record", "GDB command failed"), error=True)
        except (RpcError, ConnectionError, TimeoutError) as error:
            self._append_output(str(error), error=True)
        if command.strip().lower().startswith(("svd load", "svd read")):
            await self.refresh_svd_tree()

    async def _load_svd(self) -> None:
        """Load the target-matched SVD once and populate the peripheral tree."""
        await self.execute_command("svd load")
        await self.refresh_svd_tree()

    async def refresh_svd_tree(self) -> None:
        """Fetch structured SVD metadata and rebuild the expandable tree."""
        if not self._connected:
            return
        tree = self.query_one("#svd-tree", Tree)
        try:
            data = await self.client.request("svd.peripherals")
        except (RpcError, ConnectionError, TimeoutError) as error:
            tree.reset(f"SVD unavailable: {error}")
            return
        if not data.get("loaded"):
            tree.reset("Run svd load or svd read <file>")
            return

        tree.reset(f"{data['device']} · {len(data['peripherals'])} peripherals")
        for peripheral in data["peripherals"]:
            registers = peripheral.get("registers", [])
            label = f"{peripheral['name']}  0x{peripheral['base_address']:08X}"
            node = tree.root.add(
                label,
                data={"kind": "peripheral", "name": peripheral["name"]},
            )
            for register in registers:
                node.add_leaf(
                    f"{register['name']}  +0x{register['address_offset']:04X}",
                    data={
                        "kind": "register",
                        "peripheral": peripheral["name"],
                        "name": register["name"],
                    },
                )
        tree.root.expand()

    def on_tree_node_expanded(self, event: Tree.NodeExpanded[Any]) -> None:
        """Inspect a peripheral as soon as its tree node is opened."""
        data = event.node.data
        if isinstance(data, dict) and data.get("kind") == "peripheral":
            self.run_worker(self.execute_command(f"svd show {data['name']}"), group="commands")

    def on_tree_node_selected(self, event: Tree.NodeSelected[Any]) -> None:
        """Show the selected register detail in the central output panel."""
        data = event.node.data
        if isinstance(data, dict) and data.get("kind") == "register":
            self.run_worker(
                self.execute_command(f"svd show {data['peripheral']} {data['name']}"),
                group="commands",
            )

    async def _refresh_target_status(self) -> None:
        """Refresh target execution state and the selected debug AP/core."""
        if not self._connected or self._target_refresh_lock.locked():
            return
        async with self._target_refresh_lock:
            try:
                data = await self.client.request("target.status", timeout=5)
            except (RpcError, ConnectionError, TimeoutError):
                return
            state = str(data.get("state", "unknown")).lower()
            if state in {"stopped", "break"}:
                state_label, state_style = "BREAK · stopped", "bold yellow"
            elif state == "running":
                state_label, state_style = "RUNNING", "bold green"
            else:
                state_label, state_style = state.upper(), "bold dim"
            access_port = data.get("access_port") or "not reported"
            thread = data.get("thread_id") or "--"
            core = data.get("core") or "--"
            summary = Text.assemble(
                (f"State: {state_label}\n", state_style),
                (f"Access Port: {access_port}\n", "bold cyan"),
                (f"Core: {core}   Thread: {thread}", "dim"),
            )
            self.query_one("#target-state", Static).update(summary)

    async def action_quit(self) -> None:
        """Close the client socket and exit the dashboard."""
        await self.quit_client()

    async def on_unmount(self) -> None:
        """Release the WebSocket when the dashboard closes."""
        await self.client.close()


def _help_text(ocd_executable: str) -> list[Text]:
    """Build dashboard help with documentation links for the selected tools."""
    executable = ocd_executable.lower()
    if "pyocd" in executable:
        ocd_name = "pyOCD"
        ocd_manual = "https://pyocd.io/docs/"
    elif "openocd" in executable:
        ocd_name = "OpenOCD"
        ocd_manual = "https://openocd.org/doc/html/"
    else:
        ocd_name = ocd_executable
        ocd_manual = f"{ocd_executable} --help / its installed manual"

    return [
        Text("CLIENT COMMANDS", style="bold cyan"),
        Text("  help          Show this command reference."),
        Text("  history       List the last 40 commands; use Up/Down to browse them."),
        Text("  quit          Exit the dashboard; keep pyGdbServer, GDB, and the OCD running."),
        Text("  quit --all    Shut down pyGdbServer, GDB, and the OCD, then exit the dashboard."),
        Text("  Ctrl+Q        Same as quit."),
        Text("PYGDBTOOLKIT COMMANDS", style="bold cyan"),
        Text(
            "  lscpu         Identify the Cortex-M core, vendor, product family, memory, and UID."
        ),
        Text("  fault_info    Decode Cortex-M fault status and the stacked exception context."),
        Text("  svd load      Detect the target and load a matching CMSIS-SVD description."),
        Text("  svd read FILE Load an SVD file explicitly."),
        Text("  svd list      List peripherals from the loaded SVD."),
        Text(
            "  svd show P [R] Read a peripheral's registers, or detail register R and its bitfields."
        ),
        Text("  svd write P R VALUE  Write a register value and read it back."),
        Text("  svd monitor P R      Watch a register and report changes."),
        Text("  svd dump P|all FILE  Export a peripheral or device snapshot as JSON."),
        Text("  rtos list / select NAME  List or select a supported RTOS (Camelot)."),
        Text("  rtos load-project --from DIR  Load RTOS symbols and task metadata."),
        Text("    DIR is resolved on the GDB server, not on this client."),
        Text(
            "    For remote use, that path must also exist on the server (or be identically mounted)."
        ),
        Text(
            "    This is intentional: the same native command works in classic GDB without pyGdbServer."
        ),
        Text("  rtos show              Show the RTOS project layout."),
        Text("  rtos show task NAME    Inspect a task's live context."),
        Text("  rtos showsched N       Trace the next N scheduler elections."),
        Text("  secscan audit [FILE]   Audit target security configuration; optionally save JSON."),
        Text("  secscan report FILE    Display a saved report; see secscan help for formats."),
        Text("  Use svd help, rtos, and secscan help for command-specific syntax."),
        Text("GDB AND OCD COMMANDS", style="bold cyan"),
        Text("  gdb XXX     Run XXX as a GDB CLI command (for example: gdb info registers)."),
        Text(f"  monitor XXX Forward XXX to the selected {ocd_name} (for example: monitor help)."),
        Text(
            "  GDB manual: https://sourceware.org/gdb/current/onlinedocs/gdb.html/",
            style="link https://sourceware.org/gdb/current/onlinedocs/gdb.html/",
        ),
        Text(
            f"  {ocd_name} manual: {ocd_manual}",
            style=f"link {ocd_manual}" if ocd_manual.startswith("https://") else "",
        ),
        Text("Commands without a prefix are sent to GDB and resolve pyGdbToolkit commands."),
    ]
