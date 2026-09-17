"""`pro-mcp start`: open a GPT-6 Pro chat as a herdr agent, like `herdr agent start`.

The chat runs in terminal-browser inside a herdr tab. herdr types prompts into it
(`herdr agent prompt <name> "..."`), and Pro answers through the Local files app:
write_doc for its turn file, herdr_prompt for the handoff.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path

from .lane import herdr

CHAT_FILE_NAME = "chat-url"
PRELOAD = Path(__file__).with_name("preload.js")
DEFAULT_CHAT = "https://chatgpt.com/"
SERVICE = "com.rolfiersbox.pro-mcp"


def fail(message: str) -> SystemExit:
    return SystemExit(f"pro-mcp start: {message}")


def herdr_json(*args: str) -> dict:
    res = herdr(*args)
    if res is None or res.returncode != 0:
        detail = "herdr not runnable" if res is None else (res.stdout or res.stderr).strip()[:300]
        raise fail(f"herdr {' '.join(args[:2])} failed: {detail}")
    return json.loads(res.stdout) if res.stdout.strip() else {}


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def new_tab(workspace: str, label: str) -> str:
    out = herdr_json("tab", "create", "--workspace", workspace, "--cwd", str(Path.home() / "projects"),
                     "--label", label, "--no-focus")
    return out["result"]["root_pane"]["pane_id"]


def run_in(pane: str, command: str) -> None:
    herdr_json("pane", "send-text", pane, command)
    herdr_json("pane", "send-keys", pane, "enter")


def wait_for(check, timeout: float, what: str):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = check()
        if value:
            return value
        time.sleep(1)
    raise fail(f"timed out waiting for {what}")


def browser_bin() -> str:
    return shutil.which("terminal-browser") or str(Path.home() / ".local/bin/terminal-browser")


def browser_key(pane: str) -> str | None:
    res = subprocess.run([browser_bin(), "ls", "--all"], capture_output=True, text=True, timeout=15)
    for line in res.stdout.splitlines():
        parts = line.split()
        # "<browser key>  <workspace>:<tab>:<pane>" rows; tab rows under them are indented.
        if len(parts) > 1 and not line[0].isspace() and parts[1].endswith(f":{pane}"):
            return parts[0]
    return None


def focus_composer(key: str) -> bool:
    script = (
        "(()=>{const b=document.querySelector('#prompt-textarea');"
        "if(!b)return 'missing';b.focus();return b.contains(document.activeElement)||document.activeElement===b?'focused':'no'})()"
    )
    res = subprocess.run([browser_bin(), "action", "--browser", key, "--", "eval", script],
                         capture_output=True, text=True, timeout=15)
    return "focused" in res.stdout


def start(name: str, parent: str | None, chat: str | None, workspace: str | None, port: int, config_dir: Path) -> None:
    workspace = workspace or os.environ.get("HERDR_WORKSPACE_ID")
    if not workspace:
        raise fail("run it inside herdr or pass --workspace")
    existing = herdr("agent", "get", name)
    if existing is not None and existing.returncode == 0:
        raise fail(f"an agent named '{name}' already exists")

    chat_file = config_dir / CHAT_FILE_NAME
    if chat:
        config_dir.mkdir(parents=True, exist_ok=True)
        chat_file.write_text(chat + "\n")
    url = chat or (chat_file.read_text().strip() if chat_file.exists() else DEFAULT_CHAT)

    if not port_open(port):
        # The server normally runs all the time as a launchd job; kick it if it is down.
        subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{SERVICE}"], capture_output=True, timeout=15)
        wait_for(lambda: port_open(port), 45, f"the pro-mcp server (launchd job {SERVICE})")

    pane = new_tab(workspace, name)
    run_in(pane, "HERDR_AGENT=chatgpt " + shlex.join([browser_bin(), "open", url, f"--preload={PRELOAD}"]))
    key = wait_for(lambda: browser_key(pane), 60, "terminal-browser to open")
    wait_for(lambda: focus_composer(key), 90, "the ChatGPT message box (log in if the page asks)")

    herdr_json("agent", "rename", pane, name)
    parent = parent or os.environ.get("HERDR_PANE_ID")
    if parent:
        herdr_json("agent", "set-parent", name, parent)
    print(f"{name}: {pane}, chat {url}" + (f", parent {parent}" if parent else ""))
    print(f'prompt it with: herdr agent prompt {name} "..."')
