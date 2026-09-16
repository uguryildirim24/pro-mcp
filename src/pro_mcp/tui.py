"""Terminal UI for a Pro lane: a transcript of what Pro does plus an input line.

Text typed into the input (by Rolf, or by `herdr agent prompt`, which types and presses
Enter) is queued for Pro's wait_for_message tool.
"""

from __future__ import annotations

import threading
import time

from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input, RichLog, Static

from .lane import Event, Lane

QUIET_TOOLS = ("wait ", "reply ", "progress ")
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

KICKOFF = (
    "Kickoff for the Pro chat (paste once, in a chat with the Local files app on): "
    "\"You are herdr lane '{name}'. Follow the LANE MODE instructions of the Local files app: "
    "loop on wait_for_message and never end your turn.\""
)


class LaneApp(App):
    CSS = """
    Screen { background: #11111b; }
    #status { height: 1; padding: 0 1; background: #181825; color: #a6adc8; }
    #log { background: #11111b; padding: 0 1; scrollbar-size-vertical: 1; }
    #prompt { border: tall #313244; background: #11111b; }
    #prompt:focus { border: tall #cba6f7; }
    """
    BINDINGS = [Binding("ctrl+c", "quit", "Quit", priority=True)]

    def __init__(self, lane: Lane, connected: bool) -> None:
        super().__init__()
        self.lane = lane
        self.connected = connected
        self._frame = 0

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        yield RichLog(id="log", wrap=True, markup=False, highlight=False, auto_scroll=True)
        yield Input(placeholder=f"message for {self.lane.name} (queued for Pro)", id="prompt")

    def on_mount(self) -> None:
        self.title = f"pro · {self.lane.name}"
        log = self.query_one(RichLog)
        log.write(Text(f"GPT-6 Pro lane '{self.lane.name}'", style="bold #cba6f7"))
        if not self.connected:
            log.write(Text("local only: ChatGPT cannot reach this lane (--local)", style="#f9e2af"))
        log.write(Text(KICKOFF.format(name=self.lane.name), style="#6c7086"))
        log.write(Text(""))
        self.lane.subscribe(self._dispatch)
        self.lane.announce()
        self.query_one(Input).focus()
        self.set_interval(0.25, self._refresh_status)
        self.set_interval(1.0, self.lane.tick)

    def _dispatch(self, ev: Event) -> None:
        if threading.current_thread() is threading.main_thread():
            self._show(ev)
        else:
            self.call_from_thread(self._show, ev)

    def _show(self, ev: Event) -> None:
        log = self.query_one(RichLog)
        stamp = time.strftime("%H:%M", time.localtime(ev.at))
        if ev.kind == "incoming":
            log.write(Text(""))
            log.write(Text.assemble(("› ", "bold #89b4fa"), (ev.text, "#cdd6f4")))
        elif ev.kind == "reply":
            log.write(Text(""))
            log.write(Text.assemble(("● ", "#a6e3a1"), (f"pro {stamp}", "#6c7086")))
            log.write(Markdown(ev.text, code_theme="monokai"))
        elif ev.kind == "progress":
            log.write(Text.assemble(("  ✻ ", "#cba6f7"), (ev.text, "italic #bac2de")))
        elif ev.kind == "tool":
            if not ev.text.startswith(QUIET_TOOLS):
                log.write(Text.assemble(("  ⎿ ", "#45475a"), (ev.text, "#6c7086")))
        elif ev.kind == "notice":
            log.write(Text.assemble(("  ! ", "bold #f9e2af"), (ev.text, "#f9e2af")))
        # "state" events only change the status bar

    def _refresh_status(self) -> None:
        lane = self.lane
        self._frame = (self._frame + 1) % len(SPINNER)
        pending = lane.pending()
        queued = f" · {pending} queued" if pending else ""
        if lane.state == "working":
            mins = int((time.time() - (lane.turn_started or time.time())) // 60)
            text = f"{SPINNER[self._frame]} Pro working · {mins}m · {lane.tool_calls} calls{queued}"
            style = "#cba6f7"
        elif lane.state == "waiting":
            text = f"◌ Pro waiting for messages{queued}"
            style = "#a6e3a1"
        elif lane.state == "stalled":
            text = f"■ Pro stopped polling - type continue in the Pro chat{queued}"
            style = "#f38ba8"
        else:
            text = f"○ Pro not connected yet - paste the kickoff into a Pro chat{queued}"
            style = "#6c7086"
        self.query_one("#status", Static).update(Text(text, style=style))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if text:
            self.lane.post(text, "terminal")
