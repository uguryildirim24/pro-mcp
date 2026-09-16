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
    out = server.read_file("app/src/main.py", offset=1, limit=1000)
    assert "lines 1-400 of 500 (next: offset=401)" in out
    out = server.read_file("app/src/main.py", offset=401)
    assert "lines 401-500 of 500" in out and "next" not in out.splitlines()[0]
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
