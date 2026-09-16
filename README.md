# pro-mcp

MCP server that lets ChatGPT (Developer Mode, e.g. GPT-6 Pro) read Rolf's local project files, write markdown docs and send herdr handoffs.

- Read tools (`readOnlyHint`): `list_roots`, `list_dir`, `read_file` (whole file up to 1500 lines), `read_files` (up to 20 files per call), `search` (ripgrep with context lines), `find_files`, `git` (status/log/diff/show/branches), `herdr_agents`, `herdr_read`.
- Additive write tools (not read-only, not destructive): `write_doc` creates or appends `.md` files and never overwrites; `herdr_prompt` sends a one-line (<=600 chars) handoff to an agent pane and retries once if herdr refuses.
- For unattended runs set the app's ChatGPT permission to "Allow all actions".
- Roots: `~/projects` by default, override with `PRO_MCP_ROOTS=dir1:dir2`. Paths are resolved (symlinks included) and must stay inside a root.
- Secret-looking names (`.env*`, keys, `auth.json`, `credentials*`, `.ssh`, ...) are refused and filtered out of listings, searches and git patches.
- Access: a random secret in the URL path (`~/.config/pro-mcp/token`, mode 600). `pro-mcp rotate` replaces it.
- Exposure: `pro-mcp` sets a background `tailscale funnel` path `/` on 443 (sharing the port with other paths, e.g. wiki-mcp's `/wiki`) and a watchdog removes it when the process exits, even on SIGKILL. Every call is logged to stderr.

```bash
uv tool install --editable ~/projects/pro-mcp
pro-mcp            # serve + funnel, logs calls
pro-mcp --local    # 127.0.0.1 only
pro-mcp start      # open a Pro chat as herdr agent "pro"
pro-mcp lane       # older: serve + funnel with a lane terminal UI, Pro long-polls it
pro-mcp url        # connector URL for ChatGPT
uv run pytest -q
```

ChatGPT setup: Settings → Security and login → Developer mode on. Then Plugins → + → create an app from a remote MCP server, paste `pro-mcp url`, authentication "No authentication".

## Pro as a herdr agent

`pro-mcp start [--name pro] [--parent <pane>] [--chat <url>]`, run inside herdr, works like `herdr agent start`:

- The server runs all the time as the launchd job `com.rolfiersbox.pro-mcp` (`~/Library/LaunchAgents/com.rolfiersbox.pro-mcp.plist`, log `~/.local/state/pro-mcp/pro-mcp.log`, KeepAlive). `start` kickstarts it if the port is closed.
- It opens a tab named after the agent, running terminal-browser (the herdr plugin) on the Pro chat, reports it as kind `codex` (`HERDR_AGENT`), names it and links it to the parent (default: the calling pane).
- `--chat` is saved to `~/.config/pro-mcp/chat-url` and reused next time. Set GPT-6 Pro and turn on the Local files app in that chat once; both stick to the chat. The browser profile keeps the login.
- `preload.js` keeps focus in the message box, so `herdr agent prompt pro "..."` types into it and Enter sends.
- Pro answers through its tools: `write_doc` for the turn file, `herdr_prompt` for the DONE handoff. Nothing reads the page back.
- herdr can't see Pro's turn state (the pane shows `idle` while Pro thinks). Wait for its DONE before prompting again.

## Lane mode (older)

`pro-mcp lane [--name pro]` turns a Pro chat into a herdr lane. Run it in a herdr pane, then paste the kickoff it shows into a Pro chat with the app on.

- The pane registers with herdr as agent kind `codex` (herdr only prompts known kinds; override with `PRO_LANE_AGENT`) and names itself `--name`.
- Text typed into the pane, or sent with `herdr agent prompt pro "..."`, is queued. Pro picks it up with `wait_for_message`, reports with `progress` and `reply`, and hands off with `herdr_prompt`.
- The pane shows a live transcript and reports working/idle to herdr.
- A ChatGPT turn can't run forever. When Pro stops polling for `PRO_LANE_POLL_GRACE` seconds (default 180), the lane pushes `WAITING <name> ChatGPT turn ended - Rolf must type continue in the Pro chat` to its herdr parent (or `coordinator`).
- The lane uses the same port (`PRO_MCP_PORT`, default 8765) and funnel as `pro-mcp`, so run one or the other.
