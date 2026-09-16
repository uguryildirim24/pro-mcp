from pathlib import Path

import pytest

from pro_mcp import server
from pro_mcp.sandbox import PolicyError, Sandbox


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "root"
    (r / "app" / "src").mkdir(parents=True)
    (r / "app" / "src" / "main.py").write_text("\n".join(f"line {i}" for i in range(1, 501)))
    (r / "app" / ".env").write_text("SECRET=1\n")
    (r / "app" / "node_modules").mkdir()
    (r / "app" / "node_modules" / "x.js").write_text("needle\n")
    (r / "app" / "notes.md").write_text("a needle here\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("needle outside\n")
    (r / "link").symlink_to(outside)
    monkeypatch.setattr(server, "sandbox", Sandbox(roots=[r.resolve()]))
    return r


def test_resolve_rejects_outside_and_symlink_escape(root: Path) -> None:
    sb = server.sandbox
    with pytest.raises(PolicyError):
        sb.resolve("/etc/passwd")
    with pytest.raises(PolicyError):
        sb.resolve("../outside.txt")
    with pytest.raises(PolicyError):
        sb.resolve("link")
    with pytest.raises(PolicyError):
        sb.resolve("app/.env")
    assert sb.resolve("app/src/main.py").name == "main.py"


def test_read_file_pages(root: Path) -> None:
    out = server.read_file("app/src/main.py")
    assert "lines 1-500 of 500\n" in out
    out = server.read_file("app/src/main.py", offset=1, limit=100)
    assert "lines 1-100 of 500 (more: offset=101)" in out
    out = server.read_file("app/src/main.py", offset=401)
    assert "lines 401-500 of 500" in out and "more" not in out.splitlines()[0]
    assert server.read_file("app/.env").startswith("denied:")
    assert server.read_file("/etc/hosts").startswith("denied:")


def test_list_dir_hides_skip_and_secrets(root: Path) -> None:
    out = server.list_dir("app", depth=2)
    assert "src/" in out and "main.py" in out
    assert ".env" not in out and "node_modules" not in out


def test_search_and_find_respect_policy(root: Path) -> None:
    out = server.search("needle", ".")
    assert "notes.md" in out
    assert "node_modules" not in out and "outside" not in out
    assert "SECRET" not in server.search("SECRET", ".")
    assert "main.py" in server.find_files("*.py")
    assert ".env" not in server.find_files("*env*")


def test_git_rejects_option_refs(root: Path) -> None:
    assert server.git("app", "log", ref="--output=/tmp/x").startswith("error:")


def test_read_files_batches_and_reports_errors(root: Path) -> None:
    out = server.read_files(["app/src/main.py", "app/.env", "app/notes.md"])
    assert "lines 1-500 of 500" in out
    assert "app/.env  error:" in out
    assert "a needle here" in out


def test_search_context(root: Path) -> None:
    out = server.search("line 250$", "app/src", context=2)
    assert "line 248" in out and "line 252" in out


def test_write_doc_create_append_never_overwrite(root: Path) -> None:
    assert server.write_doc("app/docs/review-1.md", "# Review").startswith("created")
    assert (root / "app/docs/review-1.md").read_text() == "# Review\n"
    assert server.write_doc("app/docs/review-1.md", "again").startswith("error:")
    assert server.write_doc("app/docs/review-1.md", "more", append=True).startswith("appended")
    assert (root / "app/docs/review-1.md").read_text() == "# Review\nmore\n"
    assert server.write_doc("app/src/evil.py", "x").startswith("error:")
    assert server.write_doc("app/.env.md", "x").startswith("denied:")
    assert server.write_doc("/tmp/x.md", "x").startswith("denied:")


def test_herdr_prompt_retries_and_limits(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    calls: list[tuple[str, ...]] = []

    def fake(*args: str, timeout: int = 20):
        calls.append(args)
        code = 1 if len(calls) == 1 else 0
        return subprocess.CompletedProcess(args, code, stdout="", stderr="agent_not_ready")

    monkeypatch.setattr(server, "_herdr", fake)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    assert server.herdr_prompt("fable", "DONE  spec-review-1\n docs/x.md -") == "sent to fable (on retry)"
    assert calls[-1] == ("agent", "prompt", "fable", "DONE spec-review-1 docs/x.md -")
    assert server.herdr_prompt("fable", "x" * 700).startswith("error:")
