# pro-mcp

MCP server that lets ChatGPT (Developer Mode, e.g. GPT-6 Pro) read Rolf's local project files, write markdown docs and send herdr handoffs.

- Read tools (`readOnlyHint`): `list_roots`, `list_dir`, `read_file` (whole file up to 1500 lines), `read_files` (up to 20 files per call), `search` (ripgrep with context lines), `find_files`, `git` (status/log/diff/show/branches), `herdr_agents`, `herdr_read`.
- Additive write tools (not read-only, not destructive): `write_doc` creates or appends `.md` files and never overwrites; `herdr_prompt` sends a one-line (<=600 chars) handoff to an agent pane and retries once if herdr refuses.
- For unattended runs set the app's ChatGPT permission to "Allow all actions".
- Roots: `~/projects` by default, override with `PRO_MCP_ROOTS=dir1:dir2`. Paths are resolved (symlinks included) and must stay inside a root.
- Secret-looking names (`.env*`, keys, `auth.json`, `credentials*`, `.ssh`, ...) are refused and filtered out of listings, searches and git patches.
- Access: a random secret in the URL path (`~/.config/pro-mcp/token`, mode 600). `pro-mcp rotate` replaces it.
- Exposure: `pro-mcp` runs a foreground `tailscale funnel`, so the public URL exists only while the process runs. Every call is logged to stderr.

```bash
uv tool install --editable ~/projects/pro-mcp
pro-mcp            # serve + funnel, logs calls
pro-mcp --local    # 127.0.0.1 only
pro-mcp url        # connector URL for ChatGPT
uv run pytest -q
```

ChatGPT setup: Settings → Security and login → Developer mode on. Then Plugins → + → create an app from a remote MCP server, paste `pro-mcp url`, authentication "No authentication".
