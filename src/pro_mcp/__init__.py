"""pro-mcp: expose local project files to ChatGPT (Developer Mode) as read-only MCP tools.

pro-mcp            serve on 127.0.0.1 and publish it with a foreground Tailscale Funnel
pro-mcp --local    serve on 127.0.0.1 only (no funnel), for testing
pro-mcp url        print the connector URL to paste into ChatGPT
pro-mcp rotate     replace the secret in the URL (the old ChatGPT app stops working)
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pro-mcp"
TOKEN_FILE = CONFIG_DIR / "token"
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
    serve(local_only="--local" in args)


def serve(local_only: bool) -> None:
    import logging

    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    from .server import mcp, sandbox

    hosts = [f"127.0.0.1:{PORT}", f"localhost:{PORT}"]
    funnel = None
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
    print(f"pro-mcp  roots: {', '.join(sandbox.display(r) for r in sandbox.roots)}", file=sys.stderr)
    print(f"local:   http://127.0.0.1:{PORT}{mcp_path()}", file=sys.stderr)
    if public_url:
        tailscale = shutil.which("tailscale") or "/opt/homebrew/bin/tailscale"
        # Foreground funnel: the public URL only exists while this process runs.
        funnel = subprocess.Popen(
            [tailscale, "funnel", str(PORT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        print(f"public:  {public_url}", file=sys.stderr)
    print("calls:", file=sys.stderr, flush=True)

    try:
        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
    finally:
        if funnel and funnel.poll() is None:
            os.killpg(funnel.pid, signal.SIGINT)
            try:
                funnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(funnel.pid, signal.SIGKILL)
