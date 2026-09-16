"""Lane state shared by the MCP tools (worker threads) and the terminal UI (main thread).

Messages typed into the pane (by Rolf or by `herdr agent prompt`) go into the inbox;
Pro drains it with the wait_for_message tool. Everything Pro does is emitted as events
for the UI. The lane also reports working/idle to herdr and pushes WAITING to its
parent when Pro's ChatGPT turn looks finished while work is pending.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

EventKind = Literal["incoming", "reply", "progress", "tool", "notice", "state"]
LaneState = Literal["offline", "working", "waiting", "stalled"]

# Pro is considered gone when it stops polling for this long after a wait returned.
POLL_GRACE_SECONDS = int(os.environ.get("PRO_LANE_POLL_GRACE", "180"))
# herdr only lets `agent prompt` reach panes whose agent kind it knows, so the lane
# reports as one (and runs with HERDR_AGENT set to match, see run_lane).
AGENT_KIND = os.environ.get("PRO_LANE_AGENT", "codex")


@dataclass
class Event:
    kind: EventKind
    text: str
    at: float = field(default_factory=time.time)


class Lane:
    def __init__(self, name: str) -> None:
        self.name = name
        self.pane_id = os.environ.get("HERDR_PANE_ID")
        self._inbox: deque[tuple[str, str]] = deque()
        self._cond = threading.Condition()
        self._listeners: list[Callable[[Event], None]] = []
        self.state: LaneState = "offline"
        self.turn_started: float | None = None
        self.tool_calls = 0
        self.last_call: float = 0.0
        self.last_wait_end: float | None = None
        self.in_wait = False
        self._stall_pushed = False
        self._reported: str | None = None

    # ---- events -------------------------------------------------------------------
    def subscribe(self, fn: Callable[[Event], None]) -> None:
        self._listeners.append(fn)

    def emit(self, kind: EventKind, text: str) -> None:
        ev = Event(kind, text)
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:  # a broken listener must never break a tool call
                pass

    # ---- inbox ----------------------------------------------------------------------
    def post(self, text: str, source: str) -> None:
        text = text.strip()
        if not text:
            return
        with self._cond:
            self._inbox.append((source, text))
            self._cond.notify_all()
        self.emit("incoming", f"{source}: {text}" if source != "terminal" else text)
        if self.state in ("offline", "stalled"):
            self.emit("notice", "queued: Pro is not polling right now")

    def pending(self) -> int:
        with self._cond:
            return len(self._inbox)

    def wait_message(self, timeout: float) -> tuple[str, str] | None:
        self.touch()
        self.in_wait = True
        self._set_state("waiting")
        try:
            with self._cond:
                if not self._inbox:
                    self._cond.wait(timeout)
                item = self._inbox.popleft() if self._inbox else None
        finally:
            self.in_wait = False
            self.last_wait_end = time.time()
        if item is not None:
            self._set_state("working")
        return item

    # ---- activity and herdr state ------------------------------------------------------
    def touch(self) -> None:
        now = time.time()
        if self.turn_started is None or self.state in ("offline", "stalled"):
            self.turn_started = now
            self.tool_calls = 0
            if self.state == "stalled":
                self.emit("notice", "Pro is back")
        self.tool_calls += 1
        self.last_call = now
        self._stall_pushed = False
        if not self.in_wait and self.state != "working":
            self._set_state("working")

    def _set_state(self, state: LaneState) -> None:
        if state == self.state:
            return
        self.state = state
        self.emit("state", state)
        herdr_state = {"working": "working", "waiting": "idle", "stalled": "idle", "offline": "idle"}[state]
        if herdr_state != self._reported:
            self._reported = herdr_state
            threading.Thread(target=self._report, args=(herdr_state,), daemon=True).start()

    def _report(self, herdr_state: str) -> None:
        if not self.pane_id:
            return
        herdr(
            "pane", "report-agent", "--source", "pro-lane", "--agent", AGENT_KIND,
            "--state", herdr_state, "--message", self.state, self.pane_id,
        )

    def announce(self) -> None:
        """Register the pane with herdr as an idle agent named after the lane, so it can be prompted."""
        self._reported = "idle"
        threading.Thread(target=self._register, daemon=True).start()

    def _register(self) -> None:
        if not self.pane_id:
            return
        self._report("idle")
        res = herdr("agent", "rename", self.pane_id, self.name)
        if res is None or res.returncode != 0:
            err = (res.stdout or res.stderr).strip()[:200] if res is not None else "herdr not runnable"
            self.emit("notice", f"could not name this pane '{self.name}': {err}")

    def tick(self) -> None:
        """Called about once a second by the UI. Detects a finished ChatGPT turn."""
        if self.in_wait or self.state in ("offline", "stalled"):
            return
        if self.state == "waiting" and self.last_wait_end and time.time() - self.last_wait_end > POLL_GRACE_SECONDS:
            self._set_state("stalled")
            self.emit("notice", "Pro stopped polling: its ChatGPT turn probably ended. Type 'continue' in the Pro chat.")
            threading.Thread(target=self._push_stall, daemon=True).start()

    def _push_stall(self) -> None:
        if self._stall_pushed or not self.pane_id:
            return
        self._stall_pushed = True
        pending = self.pending()
        queued = f", {pending} message(s) queued" if pending else ""
        target = self.parent() or "coordinator"
        res = herdr("agent", "prompt", target, f"WAITING {self.name} ChatGPT turn ended{queued} - Rolf must type continue in the Pro chat")
        if res is not None and res.returncode == 0:
            self.emit("notice", f"pushed WAITING to {target}")
        else:
            err = (res.stdout or res.stderr).strip()[:200] if res is not None else "herdr not runnable"
            self.emit("notice", f"could not push WAITING to {target}: {err}")

    def parent(self) -> str | None:
        if not self.pane_id:
            return None
        res = herdr("agent", "get", self.pane_id)
        if res is None or res.returncode != 0:
            return None
        try:
            return json.loads(res.stdout)["result"]["agent"].get("tokens", {}).get("parent")
        except (ValueError, KeyError, TypeError, AttributeError):
            return None

    def shutdown(self) -> None:
        if self.pane_id:
            herdr("pane", "release-agent", "--source", "pro-lane", "--agent", AGENT_KIND, self.pane_id)


def herdr(*args: str, timeout: int = 20) -> subprocess.CompletedProcess[str] | None:
    exe = shutil.which("herdr") or str(Path.home() / ".local/bin/herdr")
    try:
        return subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


current: Lane | None = None
