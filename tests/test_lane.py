import threading
import time

import pytest

from pro_mcp import lane as lane_mod
from pro_mcp import server


@pytest.fixture()
def lane(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[str, ...]] = []

    class Done:
        returncode = 0
        stdout = '{"result":{"agent":{"tokens":{"parent":"w1:p1"}}}}'

    def fake_herdr(*args: str, timeout: int = 20):
        calls.append(args)
        return Done()

    monkeypatch.setattr(lane_mod, "herdr", fake_herdr)
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p9")
    ln = lane_mod.Lane("pro")
    events: list[lane_mod.Event] = []
    ln.subscribe(events.append)
    monkeypatch.setattr(lane_mod, "current", ln)
    yield ln, events, calls
    lane_mod.current = None


def test_wait_returns_posted_message(lane) -> None:
    ln, events, _ = lane
    threading.Timer(0.2, lambda: ln.post("FROM fable ROUND 2: docs/SPEC.md", "terminal")).start()
    out = server.wait_for_message(5)
    assert out == "message from terminal:\nFROM fable ROUND 2: docs/SPEC.md"
    assert ln.state == "working"
    assert server.wait_for_message(5) == "no message"
    assert ln.state == "waiting"


def test_reply_and_progress_emit(lane) -> None:
    ln, events, _ = lane
    server.progress("reading SPEC")
    server.reply("# Findings\n1. x")
    kinds = [e.kind for e in events]
    assert "progress" in kinds and "reply" in kinds


def test_stall_pushes_waiting_to_parent(lane, monkeypatch: pytest.MonkeyPatch) -> None:
    ln, events, calls = lane
    monkeypatch.setattr(lane_mod, "POLL_GRACE_SECONDS", 0)
    server.wait_for_message(5)
    ln.last_wait_end = time.time() - 1
    ln.tick()
    time.sleep(0.3)
    assert ln.state == "stalled"
    prompts = [c for c in calls if c[:2] == ("agent", "prompt")]
    assert prompts and prompts[0][2] == "w1:p1" and prompts[0][3].startswith("WAITING pro ChatGPT turn ended")
    # a new call from Pro clears the stall
    server.progress("back")
    assert ln.state == "working"


def test_tools_without_lane_fail_cleanly() -> None:
    lane_mod.current = None
    assert server.wait_for_message(5).startswith("error: no lane is running")
