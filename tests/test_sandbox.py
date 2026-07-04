"""Sandbox confinement + filesystem ops + edit_file unique-match semantics."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from atom.sandbox import LocalSandboxProvider, PathEscapeError, thread_paths


def _sandbox(thread="t"):
    return LocalSandboxProvider().acquire(thread_paths("u", thread))


def test_write_and_read_roundtrip(atom_home):
    sb = _sandbox("rw")
    sb.write_text("/mnt/user-data/workspace/a.txt", "hello\n")
    assert sb.read_text("a.txt") == "hello\n"  # relative resolves to workspace


@pytest.mark.parametrize("bad", [
    "/etc/passwd",
    "/mnt/user-data/workspace/../../../../etc/passwd",
    "../../../etc/passwd",
])
def test_path_escapes_are_blocked(atom_home, bad):
    sb = _sandbox("esc")
    with pytest.raises((PathEscapeError, FileNotFoundError)):
        sb.resolve(bad)


def test_symlink_escape_blocked(atom_home, tmp_path):
    sb = _sandbox("sym")
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    tp = thread_paths("u", "sym")
    (tp.workspace / "link").symlink_to(secret)
    with pytest.raises(PathEscapeError):
        sb.resolve("/mnt/user-data/workspace/link", must_exist=True)


def test_edit_file_unique_match(atom_home):
    sb = _sandbox("edit")
    sb.write_text("f.txt", "foo bar foo")
    with pytest.raises(ValueError):
        sb.edit_text("f.txt", "missing", "x")          # 0 matches
    with pytest.raises(ValueError):
        sb.edit_text("f.txt", "foo", "x")              # 2 matches, not unique
    assert sb.edit_text("f.txt", "bar", "BAR") == 1
    assert sb.read_text("f.txt") == "foo BAR foo"
    assert sb.edit_text("f.txt", "foo", "F", replace_all=True) == 2
    assert sb.read_text("f.txt") == "F BAR F"


def test_glob_and_grep(atom_home):
    sb = _sandbox("search")
    sb.write_text("pkg/a.py", "import os\nx = 1\n")
    sb.write_text("pkg/b.txt", "nothing here\n")
    assert any(h.endswith("a.py") for h in sb.glob("**/*.py", "/mnt/user-data/workspace"))
    hits = sb.grep("import", "/mnt/user-data/workspace")
    assert any("a.py" in h and "import os" in h for h in hits)


def test_bash_scrubs_secrets(atom_home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    sb = _sandbox("bash")
    out = sb.run_bash("echo KEY=$ANTHROPIC_API_KEY")
    assert "sk-ant-secret" not in out and "KEY=" in out


# --------------------------------------------------------------- security hardening


def test_glob_pattern_traversal_blocked(atom_home):
    sb = _sandbox("g1")
    sb.write_text("a.py", "x = 1\n")
    with pytest.raises(PathEscapeError):
        sb.glob("../*", "/mnt/user-data/workspace")
    with pytest.raises(PathEscapeError):
        sb.glob("/etc/*", "/mnt/user-data/workspace")


def test_glob_confines_symlinked_results(atom_home, tmp_path):
    sb = _sandbox("g2")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET")
    tp = thread_paths("u", "g2")
    (tp.workspace / "leak").symlink_to(secret)
    sb.write_text("real.txt", "ok\n")
    hits = sb.glob("**/*", "/mnt/user-data/workspace")
    root_real = Path(os.path.realpath(tp.workspace))
    for h in hits:
        assert Path(os.path.realpath(h)).is_relative_to(root_real), h


def test_grep_glob_traversal_blocked(atom_home):
    sb = _sandbox("g3")
    sb.write_text("a.py", "import os\n")
    with pytest.raises(PathEscapeError):
        sb.grep("import", "/mnt/user-data/workspace", glob="../*")


def test_env_scrub_drops_generic_secrets(atom_home, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_deadbeef")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-super-secret")
    monkeypatch.setenv("LANGCHAIN_API_KEY", "ls-secret")
    monkeypatch.setenv("HARMLESS_VALUE", "keepme")
    sb = _sandbox("g4")
    out = sb.run_bash("env")
    assert "ghp_deadbeef" not in out
    assert "aws-super-secret" not in out
    assert "ls-secret" not in out
    assert "keepme" in out  # non-secrets survive


def test_resolve_returns_canonical_path(atom_home):
    sb = _sandbox("g5")
    sb.write_text("a.txt", "hi\n")
    # A confined path that includes a '..' segment must canonicalize to the same physical path.
    assert sb.resolve("sub/../a.txt") == sb.resolve("a.txt")
    assert sb.resolve("a.txt") == Path(os.path.realpath(sb.resolve("a.txt")))


def test_bash_timeout_kills_and_reports(atom_home):
    sb = _sandbox("g6")
    out = sb.run_bash("sleep 5", timeout=1)
    assert "timed out" in out.lower()


def test_bash_output_is_bounded(atom_home):
    sb = _sandbox("g7")
    out = sb.run_bash("python3 -c \"print('A'*500000)\"")
    assert len(out) < 200_000  # capped well below the 500k emitted


def test_existing_workspace_allowed_roots_enforced(atom_home, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    provider = LocalSandboxProvider(allowed_workspace_roots=[allowed])
    with pytest.raises(PathEscapeError):
        provider.acquire(thread_paths("u", "g8", workspace_override=str(outside)))
    # inside an allowed root is fine
    inside = allowed / "proj"
    inside.mkdir()
    sb = provider.acquire(thread_paths("u", "g9", workspace_override=str(inside)))
    assert sb is not None
