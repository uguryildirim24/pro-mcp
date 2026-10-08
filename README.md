# pro-mcp

A Python MCP server for ChatGPT to read local project files, create or append markdown, and send herdr handoffs. It includes a herdr browser launcher and a lane terminal UI.

## Why it exists

Rolf built this bridge so a browser chat can inspect project code and leave file-based handoffs for coding agents. It avoids copying files into a chat.

herdr manages terminal panes and coding-agent sessions. terminal-browser opens a browser inside a herdr pane. Both are separate programs. Neither is bundled here.

This is developer tooling, not a validated biology research result. There are no research datasets, scientific results or associated paper in this repository.

## Run from a clean clone

Requirements: Python 3.13 or newer, uv, Git and ripgrep (`rg`) on `PATH`. Local checks use macOS. Linux and Windows have not been verified. Tailscale, ChatGPT and herdr are not needed to run the local file server.

From the directory containing a clone:

```bash
cd pro-mcp
command -v uv
command -v rg
uv sync --locked
export PRO_MCP_ROOTS="$PWD"
uv run --locked pro-mcp --local
```

Keep this terminal open. Stop the server with Ctrl+C. It binds to `127.0.0.1:8765` and prints a local credential URL to stderr. A local MCP client can use that URL. Do not copy it into an issue, screenshot, tracked file or shared log.

In another terminal, from the same clone:

```bash
uv run --locked pro-mcp --help
uv run --locked pytest -q
```

For an editable command-line installation, from the clone:

```bash
uv tool install --editable .
PRO_MCP_ROOTS="$PWD" pro-mcp --local
```

Rolf runs the editable installation from the project folder. Changes in that folder affect the installed command.

### Tools and example

All tools are registered when the server starts:

- `list_roots`, `list_dir`, `read_file`, `read_files`, `search`, `find_files`: inspect allowed text files. Reads default to 1500 lines. Batch reads accept up to 20 paths. Search and discovery use ripgrep.
- `git`: inspect status, log, diff, show and branches. `show` defaults to `HEAD`. `ref` can select a revision or range. `file` limits the path.
- `write_doc`: create a new `.md` file or append to an existing one. It never overwrites text.
- `herdr_agents`, `herdr_read`, `herdr_prompt`: list agents, read recent pane output and send a handoff. They need herdr to work.
- `wait_for_message`, `reply`, `progress`: receive lane messages and display replies or progress. They need a running lane.

An MCP client can call:

```json
{"name": "read_file", "arguments": {"path": "README.md"}}
```

`write_doc` can create `docs/review.md` with review notes. Creation fails if it already exists. Review generated files before keeping them in Git.

### Configuration

| Setting | Meaning |
| --- | --- |
| `PRO_MCP_ROOTS` | Allowed directories separated by `:`. Default `~/projects`. Relative tool paths start at the first root. Set narrow roots before exposing the server. |
| `PRO_MCP_PORT` | Loopback port, default `8765`. Use one server or lane per port. |
| `XDG_CONFIG_HOME` | Configuration base directory, default `~/.config`. Token, remembered chat URL and Funnel watchdog PID live in its `pro-mcp/` subdirectory. |
| `HERDR_WORKSPACE_ID`, `HERDR_PANE_ID` | Supplied by herdr. The launcher uses them as workspace and parent unless flags override them. |
| `PRO_LANE_AGENT` | Lane agent kind, default `codex`. herdr must recognize the kind to prompt it. |
| `PRO_LANE_POLL_GRACE` | Seconds without polling before the lane reports a stalled turn, default `180`. |

Keep private data outside exposed roots. No dataset download is required. Git ignores environments, caches, local runtime state and credential filenames. Git ignores do not prevent direct MCP reads.

## Public ChatGPT connection

This needs a signed-in Tailscale installation with Funnel permitted and a ChatGPT account that can connect to a remote MCP server.

Stop the local server first. From the clone:

```bash
export PRO_MCP_ROOTS="$PWD"
uv run --locked pro-mcp
```

The command starts the loopback server and configures a background Tailscale Funnel path `/` on HTTPS port 443. It starts a watchdog to remove that path when the process exits. Funnel exposes the endpoint to the internet, not just the tailnet. Do not use this command if another service owns that path.

In another terminal:

```bash
uv run --locked pro-mcp url
```

This prints the HTTPS connector URL with a credential in its path. Add it as a remote MCP connector in ChatGPT with no OAuth authentication. Enable it in the chosen chat and choose the model manually. Account access and interface labels can change.

Grant actions deliberately. Markdown writes change files. A herdr prompt can cause another agent to run commands outside the file roots.

Stop pro-mcp with Ctrl+C. Check `tailscale funnel status` after abnormal shutdown. No launchd job is installed by this repository.

To replace the saved token:

```bash
uv run --locked pro-mcp rotate
```

This also prints the new credential URL. Restart the running server or lane and update the connector. The running application keeps its startup path, so rotation alone does not revoke that path in a live process.

## herdr browser launcher

Install herdr and terminal-browser separately. The launcher looks on `PATH`, then in `~/.local/bin`. It expects herdr's `agent`, `tab` and `pane` commands and terminal-browser's `open`, `ls` and `action` commands. No versions have been validated end to end here.

Complete public connector setup first. With the server already running, run inside herdr:

```bash
uv run --locked pro-mcp start --name pro --chat https://chatgpt.com/
```

`--name` defaults to `pro`. `--chat` is remembered in the local configuration directory. Use a prepared chat URL to reuse its setup. `--workspace` and `--parent` override the herdr environment. New tabs start in `~/projects`.

If the server port is closed, `start` kickstarts the launchd job named in `SERVICE` at the top of `src/pro_mcp/start.py` and waits for the port to open. The job must already be installed; this repository does not install it. Change `SERVICE` to your own job label. The launcher only checks the port, not the identity of the process listening on it.

Log in through the browser if needed. The launcher names the pane as agent kind `chatgpt`, links it to its parent and focuses the composer. The chat can use `write_doc` for a turn file and `herdr_prompt` for a handoff. Nothing reads answers back from the page.

Composer focus depends on the page selector `#prompt-textarea`. herdr status detection depends on its browser integration. Idle can mean an error or a request to continue, not successful completion. Use a reviewed file and an explicit handoff as evidence of completion.

## Lane terminal UI

Lane mode is still reachable. It uses the same server port and Funnel path as the normal server, so do not run both together.

```bash
uv run --locked pro-mcp lane --name pro
```

Use `pro-mcp lane --name pro --local` for loopback-only operation. Run the lane in a herdr pane, then paste its displayed kickoff into a connected ChatGPT chat.

Messages typed into the terminal or sent by `herdr agent prompt` go into an inbox. The chat polls with `wait_for_message`, displays output with `reply` and `progress`, and sends handoffs with `herdr_prompt`.

The lane reports working or idle to herdr. When polling stops for `PRO_LANE_POLL_GRACE` seconds, it reports a stalled turn and pushes a WAITING message to its parent or `coordinator`. A ChatGPT turn cannot run forever. Rolf may need to continue the chat.

## Security and known limits

- The URL is a bearer credential. Anyone who has it can use all registered tools. There are no per-client roles or read-only credentials.
- The token file has mode `600`. The configuration directory and remembered chat URL do not receive special permissions from this program. Keep the configuration outside exposed roots.
- Startup, `url` and `rotate` print credential URLs. Tool logs include paths, patterns and prompt excerpts. The custom HTTP log redacts the startup token, but server or proxy access logs may still contain it. Treat logs as private.
- Paths are resolved through symlinks and must stay within a root. Filename checks deny `.env`, `.env.*`, key filenames, `credentials*` and similar names. They do not detect secrets in file contents or block every secret-looking filename.
- `.git` is skipped by listings and ripgrep, not denied for direct reads. Git patch filename exclusions are applied only when `file` is omitted. Git messages, revisions and explicit path requests are not a comprehensive privacy boundary.
- Herdr tools are always registered. Agent lists include pane metadata and directories. Pane output and receiving agents are outside the file sandbox.
- Reads have line, character and file-size limits. Binary data is rejected. `write_doc` cannot undo an append. Herdr prompts retry once, so inspect the target before retrying a failed handoff yourself.
- This is not an operating-system sandbox. Review the chosen projects before exposing them. Do not expose a whole home directory or allow unattended actions on private material.
- Public Funnel, ChatGPT login, connector approval, the browser launcher and herdr status detection need Rolf's review. There is no scientific validation or performance benchmark.

## Verification scope

Checked on macOS: `uv sync --locked`, CLI help and all 13 existing tests passed. `uv tool install --editable .` also passed in an isolated tool directory. The locked server and editable command both started on loopback and stopped with SIGINT. Port `8765` was occupied, so these runs used an available port through `PRO_MCP_PORT` without touching the existing listener. Runtime configuration and temporary test files stayed inside the checkout and were removed afterward.

Public Funnel, `url`, `rotate`, ChatGPT account setup, the herdr browser launcher and the interactive lane UI were not run. No GPU or large dataset download was needed. These checks do not establish external integration behavior or live token revocation.

## Project layout

```text
src/pro_mcp/__init__.py  CLI, tokens, HTTP service and Funnel lifecycle
src/pro_mcp/server.py    File, Git, markdown, herdr and lane tools
src/pro_mcp/sandbox.py   Root and filename policy
src/pro_mcp/start.py     herdr/terminal-browser launcher
src/pro_mcp/preload.js   Chat composer focus helper
src/pro_mcp/lane.py      Lane inbox, events and herdr status
src/pro_mcp/tui.py       Lane terminal UI
tests/test_tools.py      File and handoff tool tests
tests/test_lane.py       Lane tests
pyproject.toml          Package metadata and dependencies
uv.lock                 Locked dependencies
LICENSE                 MIT license
```

## How this was built

AI coding agents did much of the implementation under Rolf's direction. Rolf set the goal of connecting local project files and coding-agent handoffs to a browser chat. Rolf requested public-release cleanup without agent commits or pushes. The local checks described here do not establish external integration behavior. Rolf's review of the final changes and external integrations is still pending.

## License

MIT. See `LICENSE`. External programs and dependencies retain their own licenses. No herdr source is redistributed here.
