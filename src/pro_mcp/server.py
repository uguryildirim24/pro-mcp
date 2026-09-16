"""The MCP server: a handful of narrow, read-only tools over local files."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .sandbox import SKIP_DIRS, DENY_NAMES, PolicyError, Sandbox, is_denied_name

MAX_READ_LINES = 400
MAX_LINE_CHARS = 2000
MAX_FILE_BYTES = 5_000_000
MAX_LIST_ENTRIES = 400
MAX_SEARCH_RESULTS = 200
MAX_GIT_LINES = 600

INSTRUCTIONS = """\
Read-only access to Rolf's local project files on his Mac.
Paths can be absolute, start with ~, or be relative to the first root.
Start with list_roots or list_dir, use search/find_files to locate code, then read_file
in chunks (offset/limit, at most 400 lines per call). Nothing here can modify files.
Secret-looking files (.env, keys, auth.json, ...) are refused on purpose."""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

sandbox = Sandbox.from_env()
mcp = MCPServer(name="pro-files", title="Local project files (read-only)", instructions=INSTRUCTIONS)


def log(tool: str, detail: str, started: float, outcome: str = "ok") -> None:
    ms = int((time.monotonic() - started) * 1000)
    print(f"{time.strftime('%H:%M:%S')}  {tool:<10} {outcome:<7} {ms:>5}ms  {detail}", file=sys.stderr, flush=True)


def guarded(tool: str, detail: str, fn):
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


@mcp.tool(title="Read file", annotations=READ_ONLY)
def read_file(path: str, offset: int = 1, limit: int = 200) -> str:
    """Read a text file with line numbers. offset is 1-based; limit is at most 400 lines.
    The header says how many lines the file has, so you can page through it."""

    def run() -> str:
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
        chunk = all_lines[start - 1 : start - 1 + count]
        end = start + len(chunk) - 1
        body = "\n".join(
            f"{n:>6}\t{line[:MAX_LINE_CHARS]}" for n, line in enumerate(chunk, start=start)
        )
        more = f" (next: offset={end + 1})" if end < len(all_lines) else ""
        return f"{sandbox.display(real)}  lines {start}-{end} of {len(all_lines)}{more}\n{body}"

    return guarded("read_file", f"{path} @{offset}+{limit}", run)


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
def search(pattern: str, path: str = ".", glob: str | None = None, ignore_case: bool = False, max_results: int = 50) -> str:
    """Search file contents with a ripgrep regex. Returns path:line:text matches.
    glob narrows files, e.g. '*.rs' or 'src/**/*.ts'. Respects .gitignore."""

    def run() -> str:
        base = sandbox.resolve(path)
        n = max(1, min(max_results, MAX_SEARCH_RESULTS))
        cmd = ["rg", "--line-number", "--no-heading", "--color=never", "--max-columns=300", "--max-count=20"]
        if ignore_case:
            cmd.append("-i")
        if glob:
            cmd += ["--glob", glob]
        # later globs win in ripgrep, so the excludes must come after the caller's glob
        cmd += _rg_excludes()
        cmd += ["--regexp", pattern, "--", str(base)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode == 2:
            raise ValueError(proc.stderr.strip()[:500])
        home = str(Path.home())
        lines = [l.replace(home, "~", 1) for l in proc.stdout.splitlines() if not _denied_path(l.split(":", 1)[0])]
        head = lines[:n]
        tail = f"\n[{len(lines) - n} more matches not shown]" if len(lines) > n else ""
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
