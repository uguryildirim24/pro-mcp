"""pro-mcp: give ChatGPT (Developer Mode, e.g. GPT-6 Pro) Rolf's local files and a herdr lane.

pro-mcp                  serve on 127.0.0.1 and publish it with a foreground Tailscale Funnel
pro-mcp --local          serve on 127.0.0.1 only (no funnel), for testing
pro-mcp lane [--name N]  serve + funnel with a terminal UI: run it in a herdr pane, and Pro
                         becomes lane N (default "pro") that other agents can prompt
pro-mcp start [--name N] [--parent P] [--chat URL]
                         open a Pro chat in terminal-browser as herdr agent N (default "pro"),
                         starting the server if needed. --chat is remembered for next time
pro-mcp url              print the connector URL to paste into ChatGPT
pro-mcp rotate           replace the secret in the URL (the old ChatGPT app stops working)
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pro-mcp"
TOKEN_FILE = CONFIG_DIR / "token"
WATCHDOG_PID = CONFIG_DIR / "funnel-watchdog.pid"
PORT = int(os.environ.get("PRO_MCP_PORT", "8765"))


def token(create: bool = True) -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    if not create:
        raise SystemExit("no token yet; run pro-mcp once")
    return rotate_token()


def rotate_token() -> str:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(value + "\n")
    TOKEN_FILE.chmod(0o600)
    return value


def tailnet_host() -> str:
    tailscale = shutil.which("tailscale") or "/opt/homebrew/bin/tailscale"
    out = subprocess.run([tailscale, "status", "--json"], capture_output=True, text=True, check=True).stdout
    return json.loads(out)["Self"]["DNSName"].rstrip(".")


def mcp_path() -> str:
    return f"/{token()}/mcp"


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return
    if args and args[0] == "url":
        print(f"https://{tailnet_host()}{mcp_path()}")
        return
    if args and args[0] == "rotate":
        rotate_token()
        print(f"new URL: https://{tailnet_host()}{mcp_path()}")
        return
    if args and args[0] == "start":
        from .start import start

        def opt(flag: str) -> str | None:
            return args[args.index(flag) + 1] if flag in args else None

        start(opt("--name") or "pro", opt("--parent"), opt("--chat"), opt("--workspace"), PORT, CONFIG_DIR)
        return
    if args and args[0] == "lane":
        name = args[args.index("--name") + 1] if "--name" in args else "pro"
        run_lane(name, local_only="--local" in args)
        return
    serve(local_only="--local" in args)


def access_log(app, secret: str, sink: Callable[[str, int], None]):
    """ASGI wrapper that reports one line per HTTP request, with the secret redacted."""

    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        body = bytearray()
        status = {"code": 0}

        async def recv():
            msg = await receive()
            if msg.get("type") == "http.request" and len(body) < 4096:
                body.extend(msg.get("body", b"")[:4096])
            return msg

        async def snd(msg):
            if msg["type"] == "http.response.start":
                status["code"] = msg["status"]
            await send(msg)

        try:
            await app(scope, recv, snd)
        finally:
            method = ""
            if body:
                try:
                    method = json.loads(body).get("method", "")
                except (ValueError, AttributeError):
                    method = "?"
            path = scope["path"].replace(secret, "<token>")
            sink(
                f"{time.strftime('%H:%M:%S')}  http {scope['method']} {path} -> {status['code']} {method}"
                f"  ua={headers.get('user-agent', '')[:40]!r}",
                status["code"],
            )

    return wrapped


def build_app(local_only: bool, sink: Callable[[str, int], None]):
    import logging

    from mcp.server.transport_security import TransportSecuritySettings

    from .server import mcp

    hosts = [f"127.0.0.1:{PORT}", f"localhost:{PORT}"]
    public_url = None
    if not local_only:
        host = tailnet_host()
        hosts.append(host)
        public_url = f"https://{host}{mcp_path()}"
    app = mcp.streamable_http_app(
        streamable_http_path=mcp_path(),
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=["https://chatgpt.com", "https://chat.openai.com"],
        ),
    )
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)
    return access_log(app, token(), sink), public_url


def start_funnel() -> subprocess.Popen:
    tailscale = shutil.which("tailscale") or "/opt/homebrew/bin/tailscale"
    stop_stale_watchdog()
    # A background funnel path on 443, so it can share the port with other paths
    # (wiki-mcp holds /wiki). A foreground funnel is refused once 443 has a listener.
    res = subprocess.run(
        [tailscale, "funnel", "--bg", "--https=443", "--set-path=/", str(PORT)],
        capture_output=True, text=True, timeout=30,
    )
    if res.returncode != 0:
        raise SystemExit(f"tailscale funnel failed: {(res.stderr or res.stdout).strip()}")
    # Background paths outlive their process, so a watchdog shell removes ours when
    # pro-mcp exits, even on SIGKILL: the public URL only exists while pro-mcp runs.
    off = f'"{tailscale}" funnel --https=443 --set-path=/ off >/dev/null 2>&1'
    watchdog = (
        f"trap '{off}' EXIT; trap 'exit 0' HUP INT TERM; "
        f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 1; done"
    )
    proc = subprocess.Popen(["/bin/sh", "-c", watchdog], start_new_session=True)
    WATCHDOG_PID.write_text(f"{proc.pid}\n")
    return proc


def stop_stale_watchdog() -> None:
    """Let a watchdog left by a killed pro-mcp remove its path before we set ours,
    or it would remove ours a second later."""
    try:
        pid = int(WATCHDOG_PID.read_text())
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
        if "--set-path=/ off" not in cmd:  # gone, or the pid now belongs to something else
            return
        os.killpg(pid, signal.SIGTERM)
    except (OSError, ValueError):
        return
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            os.killpg(pid, 0)
        except OSError:
            return
        time.sleep(0.2)


def stop_funnel(funnel: subprocess.Popen | None) -> None:
    if funnel and funnel.poll() is None:
        os.killpg(funnel.pid, signal.SIGTERM)
        try:
            funnel.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(funnel.pid, signal.SIGKILL)


def serve(local_only: bool) -> None:
    import uvicorn

    from .server import sandbox

    app, public_url = build_app(local_only, lambda line, _code: print(line, file=sys.stderr, flush=True))
    print(f"pro-mcp  roots: {', '.join(sandbox.display(r) for r in sandbox.roots)}", file=sys.stderr)
    print(f"local:   http://127.0.0.1:{PORT}{mcp_path()}", file=sys.stderr)
    funnel = start_funnel() if public_url else None
    if public_url:
        print(f"public:  {public_url}", file=sys.stderr)
    print("calls:", file=sys.stderr, flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
    finally:
        stop_funnel(funnel)


def run_lane(name: str, local_only: bool) -> None:
    import threading

    import uvicorn

    from . import lane as lane_mod
    from .tui import LaneApp

    if os.environ.get("HERDR_PANE_ID") and os.environ.get("HERDR_AGENT") != lane_mod.AGENT_KIND:
        # herdr reads HERDR_AGENT from the process's initial environment, so re-exec with it.
        os.execvpe(sys.argv[0], sys.argv, {**os.environ, "HERDR_AGENT": lane_mod.AGENT_KIND})
    lane = lane_mod.Lane(name)
    lane_mod.current = lane

    def sink(line: str, code: int) -> None:
        if code >= 400:
            lane.emit("notice", line)

    app, public_url = build_app(local_only, sink)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="critical"))
    thread = threading.Thread(target=server.run, name="pro-mcp-http", daemon=True)
    thread.start()
    funnel = start_funnel() if public_url else None
    try:
        LaneApp(lane, connected=public_url is not None).run()
    finally:
        server.should_exit = True
        lane.shutdown()
        stop_funnel(funnel)
        thread.join(timeout=5)
