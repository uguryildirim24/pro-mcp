"""The MCP server: a handful of narrow, read-only tools over local files."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import lane as lane_mod
from .sandbox import SKIP_DIRS, DENY_NAMES, PolicyError, Sandbox, is_denied_name

MAX_READ_LINES = 2000
DEFAULT_READ_LINES = 1500
MAX_READ_CHARS = 150_000
MAX_BATCH_CHARS = 250_000
MAX_LINE_CHARS = 2000
MAX_DOC_BYTES = 400_000
MAX_HANDOFF_CHARS = 600
MAX_FILE_BYTES = 5_000_000
MAX_LIST_ENTRIES = 400
MAX_SEARCH_RESULTS = 200
MAX_GIT_LINES = 600

INSTRUCTIONS = """\
Read access to Rolf's local project files on his Mac, plus two write tools: write_doc (create or append markdown) and herdr_prompt (message another agent pane).
Paths can be absolute, start with ~, or be relative to the first root.
Read efficiently: read_file returns whole files (up to 1500 lines) by default, read_files
takes many paths in one call, and search returns context lines. Do not re-read a file you
already have; page with offset only when the header says there is more.
write_doc creates or appends to markdown files only; it never overwrites existing text.
herdr_prompt sends a one-line message to another agent pane (e.g. a DONE handoff).

LANE MODE (when wait_for_message works): you are a herdr lane named in the kickoff.
Loop forever inside this one turn: call wait_for_message; when it returns a message, do the
work, narrate with progress (short, every few minutes of work), put anything a human should
read in reply (markdown), send exactly one herdr_prompt handoff (DONE/WAITING or the
protocol line the message asks for), then call wait_for_message again. When it returns
"no message", call it again immediately. Never end your turn on your own.
Secret-looking files (.env, keys, auth.json, ...) are refused on purpose."""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
# Honest annotations: these change state but never destroy existing content.
ADDITIVE_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)

sandbox = Sandbox.from_env()
mcp = MCPServer(name="pro-files", title="Local project files and herdr", instructions=INSTRUCTIONS)


def log(tool: str, detail: str, started: float, outcome: str = "ok") -> None:
    ms = int((time.monotonic() - started) * 1000)
    if lane_mod.current is not None:
        lane_mod.current.emit("tool", f"{tool} {detail}" + ("" if outcome == "ok" else f"  [{outcome}]"))
        return
    print(f"{time.strftime('%H:%M:%S')}  {tool:<10} {outcome:<7} {ms:>5}ms  {detail}", file=sys.stderr, flush=True)


def guarded(tool: str, detail: str, fn):
    if lane_mod.current is not None:
        lane_mod.current.touch()
    started = time.monotonic()
    try:
        out = fn()
    except PolicyError as e:
        log(tool, detail, started, "denied")
        return f"denied: {e}"
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        log(tool, detail, started, "error")
        return f"error: {e}"
    log(tool, detail, started)
    return out


@mcp.tool(title="List roots", annotations=READ_ONLY)
def list_roots() -> str:
    """List the directories this server may read."""
    return guarded("roots", "", lambda: "\n".join(sandbox.display(r) for r in sandbox.roots))


@mcp.tool(title="List directory", annotations=READ_ONLY)
def list_dir(path: str = ".", depth: int = 1) -> str:
    """List a directory. depth 1-3. Skips .git, node_modules, build output and secret-looking files.
    Directories end with '/', files show their size in bytes."""

    def run() -> str:
        base = sandbox.resolve(path)
        if not base.is_dir():
            raise ValueError(f"{path} is not a directory")
        lines: list[str] = []

        def walk(d: Path, level: int) -> None:
            try:
                entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except PermissionError:
                return
            for e in entries:
                if len(lines) >= MAX_LIST_ENTRIES:
                    return
                if e.name in SKIP_DIRS or is_denied_name(e.name):
                    continue
                indent = "  " * level
                if e.is_dir() and not e.is_symlink():
                    lines.append(f"{indent}{e.name}/")
                    if level + 1 < depth:
                        walk(e, level + 1)
                else:
                    try:
                        size = e.stat().st_size
                    except OSError:
                        size = -1
                    lines.append(f"{indent}{e.name}  {size}")

        walk(base, 0)
        depth_note = "" if len(lines) < MAX_LIST_ENTRIES else f"\n[truncated at {MAX_LIST_ENTRIES} entries]"
        return f"{sandbox.display(base)}/\n" + "\n".join(lines) + depth_note

    depth = max(1, min(depth, 3))
    return guarded("list_dir", f"{path} depth={depth}", run)


def _read_text(path: str, offset: int, limit: int, char_budget: int) -> str:
    real = sandbox.resolve(path)
    if not real.is_file():
        raise ValueError(f"{path} is not a file")
    if real.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"{path} is larger than {MAX_FILE_BYTES} bytes")
    data = real.read_bytes()
    if b"\x00" in data[:8000]:
        raise ValueError(f"{path} looks binary")
    all_lines = data.decode("utf-8", errors="replace").splitlines()
    start = max(1, offset)
    count = max(1, min(limit, MAX_READ_LINES))
    out: list[str] = []
    used = 0
    n = start
    for line in all_lines[start - 1 : start - 1 + count]:
        row = f"{n:>6}\t{line[:MAX_LINE_CHARS]}"
        if out and used + len(row) + 1 > char_budget:
            break
        out.append(row)
        used += len(row) + 1
        n += 1
    end = start + len(out) - 1
    more = f" (more: offset={end + 1})" if end < len(all_lines) else ""
    return f"=== {sandbox.display(real)}  lines {start}-{end} of {len(all_lines)}{more}\n" + "\n".join(out)


@mcp.tool(title="Read file", annotations=READ_ONLY)
def read_file(path: str, offset: int = 1, limit: int = DEFAULT_READ_LINES) -> str:
    """Read a text file with line numbers. By default returns the whole file up to 1500 lines
    (max 2000 per call, ~150k chars). The header says if there is more and which offset to use."""
    return guarded("read_file", f"{path} @{offset}+{limit}", lambda: _read_text(path, offset, limit, MAX_READ_CHARS))


@mcp.tool(title="Read several files", annotations=READ_ONLY)
def read_files(paths: list[str], max_lines_each: int = DEFAULT_READ_LINES) -> str:
    """Read up to 20 text files in one call (whole files up to max_lines_each lines each,
    ~250k chars total). Prefer this over many read_file calls."""

    def run() -> str:
        parts: list[str] = []
        budget = MAX_BATCH_CHARS
        for path in paths[:20]:
            if budget < 2000:
                parts.append(f"=== {path}  skipped: batch size limit reached, read it separately")
                continue
            try:
                text = _read_text(path, 1, max_lines_each, budget)
            except (PolicyError, OSError, ValueError) as e:
                text = f"=== {path}  error: {e}"
            parts.append(text)
            budget -= len(text)
        if len(paths) > 20:
            parts.append(f"[{len(paths) - 20} paths ignored: at most 20 per call]")
        return "\n\n".join(parts)

    return guarded("read_files", f"{len(paths)} files", run)


_RG_PREFIX = re.compile(r"^(.*?)(?::\d+:|-\d+-)")


def _denied_path(path: str) -> bool:
    return any(is_denied_name(part) or part in SKIP_DIRS for part in Path(path).parts)


def _rg_excludes() -> list[str]:
    args: list[str] = []
    for d in SKIP_DIRS:
        args += ["--glob", f"!{d}/"]
    for pat in DENY_NAMES:
        args += ["--glob", f"!{pat}"]
    return args


@mcp.tool(title="Search file contents", annotations=READ_ONLY)
def search(pattern: str, path: str = ".", glob: str | None = None, ignore_case: bool = False, context: int = 3, max_results: int = 80) -> str:
    """Search file contents with a ripgrep regex. Returns path:line:text matches with `context`
    lines around each match (0-10), so you often don't need a follow-up read.
    glob narrows files, e.g. '*.rs' or 'src/**/*.ts'. Respects .gitignore."""

    def run() -> str:
        base = sandbox.resolve(path)
        n = max(1, min(max_results, MAX_SEARCH_RESULTS))
        cmd = ["rg", "--line-number", "--no-heading", "--color=never", "--max-columns=300", "--max-count=20"]
        if ignore_case:
            cmd.append("-i")
        if context > 0:
            cmd += ["--context", str(min(context, 10))]
        if glob:
            cmd += ["--glob", glob]
        # later globs win in ripgrep, so the excludes must come after the caller's glob
        cmd += _rg_excludes()
        cmd += ["--regexp", pattern, "--", str(base)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode == 2:
            raise ValueError(proc.stderr.strip()[:500])
        home = str(Path.home())
        lines = []
        for l in proc.stdout.splitlines():
            m = _RG_PREFIX.match(l)
            if l != "--" and m and _denied_path(m.group(1)):
                continue
            lines.append(l.replace(home, "~", 1))
        limit = n * (1 + 2 * min(max(context, 0), 10))
        head = lines[:limit]
        tail = f"\n[{len(lines) - limit} more lines not shown; narrow the search]" if len(lines) > limit else ""
        return ("\n".join(head) + tail) if head else "no matches"

    return guarded("search", f"/{pattern}/ in {path}" + (f" glob={glob}" if glob else ""), run)


@mcp.tool(title="Find files", annotations=READ_ONLY)
def find_files(glob: str, path: str = ".", max_results: int = 100) -> str:
    """Find files by name glob, e.g. '*.md', '**/config*.toml'. Respects .gitignore."""

    def run() -> str:
        base = sandbox.resolve(path)
        n = max(1, min(max_results, MAX_SEARCH_RESULTS))
        cmd = ["rg", "--files", "--color=never", "--glob", glob, *_rg_excludes(), "--", str(base)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode == 2:
            raise ValueError(proc.stderr.strip()[:500])
        home = str(Path.home())
        files = sorted(l.replace(home, "~", 1) for l in proc.stdout.splitlines() if not _denied_path(l))
        tail = f"\n[{len(files) - n} more not shown]" if len(files) > n else ""
        return ("\n".join(files[:n]) + tail) if files else "no files"

    return guarded("find", f"{glob} in {path}", run)


GIT_COMMANDS: dict[str, list[str]] = {
    "status": ["status", "--short", "--branch"],
    "log": ["log", "--oneline", "--decorate", "-n", "40"],
    "diff": ["diff", "--stat", "--patch"],
    "show": ["show", "--stat", "--patch"],
    "branches": ["branch", "-vv", "--all"],
}


@mcp.tool(title="Git (read-only)", annotations=READ_ONLY)
def git(repo: str, command: Literal["status", "log", "diff", "show", "branches"], ref: str | None = None, file: str | None = None) -> str:
    """Run a read-only git command in a repo: status, log, diff (ref optional, e.g. 'main..HEAD'),
    show (ref required), branches. file limits log/diff/show to one path."""

    def run() -> str:
        real = sandbox.resolve(repo)
        cmd = ["git", "-C", str(real), "--no-pager", "-c", "core.quotepath=off", *GIT_COMMANDS[command]]
        if ref:
            if ref.startswith("-") or any(c.isspace() for c in ref):
                raise ValueError("ref must be a plain revision like HEAD~2 or main..HEAD")
            if command == "status" or command == "branches":
                raise ValueError(f"{command} takes no ref")
            cmd.append(ref)
        elif command == "show":
            cmd.append("HEAD")
        if file:
            target = sandbox.resolve(str(real / file) if not Path(file).is_absolute() else file)
            cmd += ["--", str(target)]
        elif command in ("diff", "show"):
            # keep committed secrets out of patches
            cmd += ["--", ".", *(f":(exclude,glob)**/{pat}" for pat in DENY_NAMES)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env={"GIT_OPTIONAL_LOCKS": "0", "PATH": "/usr/bin:/opt/homebrew/bin"})
        if proc.returncode != 0:
            raise ValueError(proc.stderr.strip()[:500])
        lines = proc.stdout.splitlines()
        tail = f"\n[{len(lines) - MAX_GIT_LINES} more lines not shown]" if len(lines) > MAX_GIT_LINES else ""
        return "\n".join(lines[:MAX_GIT_LINES]) + tail or "(empty)"

    return guarded("git", f"{command} {repo}" + (f" {ref}" if ref else "") + (f" -- {file}" if file else ""), run)


@mcp.tool(title="Write markdown doc", annotations=ADDITIVE_WRITE)
def write_doc(path: str, content: str, append: bool = False) -> str:
    """Create a new markdown (.md) file, or append to one with append=true. Never overwrites
    existing text: creating a file that already exists fails. Parent folders are created.
    Use it for review files, turn files and notes, e.g. docs/spec-review-3.md."""

    def run() -> str:
        real = sandbox.resolve(path)
        if real.suffix.lower() != ".md":
            raise ValueError("write_doc only writes .md files")
        data = content if content.endswith("\n") else content + "\n"
        if len(data.encode()) > MAX_DOC_BYTES:
            raise ValueError(f"content is larger than {MAX_DOC_BYTES} bytes")
        if append:
            if not real.is_file():
                raise ValueError(f"{path} does not exist; create it first")
            with real.open("a", encoding="utf-8") as f:
                f.write(data)
            verb = "appended"
        else:
            real.parent.mkdir(parents=True, exist_ok=True)
            # re-check after mkdir so a symlinked parent can't escape the roots
            real = sandbox.resolve(str(real))
            with real.open("x", encoding="utf-8") as f:
                f.write(data)
            verb = "created"
        return f"{verb} {sandbox.display(real)} ({len(data.encode())} bytes)"

    return guarded("write_doc", f"{path} {'append' if append else 'create'} {len(content)}ch", run)


def _herdr(*args: str, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    herdr = shutil.which("herdr") or str(Path.home() / ".local/bin/herdr")
    return subprocess.run([herdr, *args], capture_output=True, text=True, timeout=timeout)


@mcp.tool(title="List herdr agents", annotations=READ_ONLY)
def herdr_agents() -> str:
    """List agent panes in Rolf's herdr session: name, status (working/idle/blocked/done), agent kind,
    lane tokens and working directory. Use the name with herdr_read or herdr_prompt."""

    def run() -> str:
        proc = _herdr("agent", "list")
        if proc.returncode != 0:
            raise ValueError((proc.stderr or proc.stdout).strip()[:500])
        agents = json.loads(proc.stdout)["result"]["agents"]
        home = str(Path.home())
        rows = []
        for a in agents:
            tokens = " ".join(f"{k}={v}" for k, v in (a.get("tokens") or {}).items())
            cwd = (a.get("cwd") or "").replace(home, "~", 1)
            rows.append(f"{a.get('name') or a.get('pane_id')}  {a.get('agent_status')}  {a.get('agent')}  {tokens}  {cwd}".rstrip())
        return "\n".join(rows) or "no agents"

    return guarded("h_agents", "", run)


@mcp.tool(title="Read herdr agent output", annotations=READ_ONLY)
def herdr_read(agent: str, lines: int = 80) -> str:
    """Read the recent terminal output of an agent pane (at most 400 lines)."""

    def run() -> str:
        proc = _herdr("agent", "read", agent, "--source", "recent-unwrapped", "--lines", str(max(1, min(lines, 400))))
        if proc.returncode != 0:
            raise ValueError((proc.stderr or proc.stdout).strip()[:500])
        try:
            return json.loads(proc.stdout)["result"].get("text", proc.stdout)
        except (ValueError, KeyError, TypeError):
            return proc.stdout

    return guarded("h_read", f"{agent} {lines}", run)


@mcp.tool(title="Send herdr handoff", annotations=ADDITIVE_WRITE)
def herdr_prompt(agent: str, message: str) -> str:
    """Send a one-line message to another agent pane, e.g.
    'DONE spec-review-3 docs/spec-review-3.md -' or 'FROM pro ROUND 3: docs/spec-review-3.md - 5 findings'.
    Max 600 characters, no newlines. Point at files instead of pasting content.
    Returns an error if the target is blocked or not ready; it is retried once."""

    def run() -> str:
        text = " ".join(message.split())
        if not text:
            raise ValueError("empty message")
        if len(text) > MAX_HANDOFF_CHARS:
            raise ValueError(f"message is longer than {MAX_HANDOFF_CHARS} chars; point at a file instead")
        last = ""
        for attempt in (1, 2):
            proc = _herdr("agent", "prompt", agent, text)
            if proc.returncode == 0:
                return f"sent to {agent}" + (" (on retry)" if attempt == 2 else "")
            last = (proc.stderr or proc.stdout).strip()[:500]
            if attempt == 1:
                time.sleep(5)
        raise ValueError(f"herdr refused the prompt to {agent}: {last}")

    return guarded("h_prompt", f"{agent}: {message[:80]}", run)


def _lane() -> "lane_mod.Lane":
    if lane_mod.current is None:
        raise ValueError("no lane is running: start pro-mcp with `pro-mcp lane`")
    return lane_mod.current


@mcp.tool(title="Wait for the next lane message", annotations=ADDITIVE_WRITE)
def wait_for_message(timeout_seconds: int = 45) -> str:
    """Block until a message arrives for this lane (typed in its herdr pane or sent with
    `herdr agent prompt`), up to timeout_seconds (5-55). Returns the message, or
    'no message' on timeout. Takes the message off the queue. Call it in a loop."""

    def run() -> str:
        item = _lane().wait_message(max(5, min(timeout_seconds, 55)))
        if item is None:
            return "no message"
        source, text = item
        return f"message from {source}:\n{text}"

    return guarded("wait", f"{timeout_seconds}s", run)


@mcp.tool(title="Show a reply in the lane terminal", annotations=ADDITIVE_WRITE)
def reply(markdown: str) -> str:
    """Show text in the lane's terminal pane, rendered as markdown. Use it for every answer
    a human or another agent should be able to read (they read the pane, not ChatGPT)."""

    def run() -> str:
        _lane().emit("reply", markdown)
        return "shown"

    return guarded("reply", f"{len(markdown)}ch", run)


@mcp.tool(title="Post a progress note", annotations=ADDITIVE_WRITE)
def progress(note: str) -> str:
    """Post a one-line progress note to the lane terminal, e.g. 'reading SPEC §3-5' or
    'drafting findings 4/9'. Use it every few minutes during long work."""

    def run() -> str:
        _lane().emit("progress", " ".join(note.split())[:300])
        return "ok"

    return guarded("progress", note[:60], run)
