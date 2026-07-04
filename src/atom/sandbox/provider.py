"""LocalSandboxProvider — the local (no-docker) execution backend.

Every sandbox/filesystem tool goes through a :class:`LocalSandbox`, which:
  * resolves virtual ``/mnt/...`` paths (or workspace-relative paths) to confined
    physical paths, rejecting ``..`` / symlink / out-of-mount escapes;
  * serializes read-modify-write per ``(sandbox_id, path)`` via a lock registry;
  * runs ``bash`` (opt-in), ``glob``, ``grep``, and directory listing.

This is the **docker seam**: Phase 2 swaps ``LocalSandboxProvider`` for a
``DockerSandboxProvider`` with the same interface and the middleware chain is unchanged.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any

from atom.sandbox.paths import VIRTUAL_WORKSPACE, ThreadPaths

_OUTPUT_CAP = 60_000  # bytes of stdout/stderr surfaced to the model
_READ_MAX_BYTES = 2_000_000  # refuse to read files larger than this as text


class PathEscapeError(ValueError):
    """Raised when a requested path resolves outside its sandbox mount."""


def _has_traversal(pattern: str | None) -> bool:
    """True if a glob pattern is absolute or contains a ``..`` segment (would escape the root)."""
    if not pattern:
        return False
    if pattern.startswith("/"):
        return True
    return ".." in re.split(r"[\\/]", pattern)


class LocalSandbox:
    """A confined view of one thread's directories."""

    def __init__(self, sandbox_id: str, path_mappings: dict[str, Path], *, bash_enabled: bool):
        self.id = sandbox_id
        # Longest prefixes first so /mnt/user-data/workspace wins over a hypothetical /mnt.
        self.path_mappings = dict(
            sorted(path_mappings.items(), key=lambda kv: len(kv[0]), reverse=True)
        )
        self.bash_enabled = bash_enabled
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------------ paths
    def resolve(self, path: str, *, must_exist: bool = False) -> Path:
        """Map a virtual/relative path to a confined physical path.

        Rules: a path under a known virtual mount maps to that mount's dir; a bare
        relative path is taken relative to the workspace; any other absolute path is
        rejected. The realpath must stay within the (realpath of the) mount root.
        """
        p = path.strip()
        mapped_root: Path | None = None
        rel = ""
        for prefix, phys in self.path_mappings.items():
            if p == prefix or p.startswith(prefix.rstrip("/") + "/"):
                mapped_root = phys
                rel = p[len(prefix):].lstrip("/")
                break
        if mapped_root is None:
            if p.startswith("/"):
                raise PathEscapeError(
                    f"Path '{path}' is outside the sandbox mounts "
                    f"({', '.join(self.path_mappings)}). Use a virtual path like "
                    f"{VIRTUAL_WORKSPACE}/file.txt."
                )
            mapped_root = self.path_mappings[VIRTUAL_WORKSPACE]
            rel = p

        candidate = mapped_root / rel if rel else mapped_root
        root_real = Path(os.path.realpath(mapped_root))
        cand_real = Path(os.path.realpath(candidate))
        if cand_real != root_real and not cand_real.is_relative_to(root_real):
            raise PathEscapeError(f"Path '{path}' escapes its sandbox mount.")
        if must_exist and not cand_real.exists():
            raise FileNotFoundError(f"Path not found: {path}")
        # Return the canonicalized path (symlinks/.. resolved): the path we validated is the path
        # callers open and lock, closing the TOCTOU/symlink-re-follow gap and de-aliasing locks.
        return cand_real

    def file_lock(self, resolved: Path) -> threading.Lock:
        """A stable per-(sandbox, path) lock so concurrent edits serialize."""
        key = f"{self.id}:{resolved}"
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = threading.Lock()
            return lock

    @staticmethod
    def _within(root_real: Path, p: Path) -> bool:
        """True if ``p`` (after symlink resolution) stays inside ``root_real``."""
        pr = Path(os.path.realpath(p))
        return pr == root_real or pr.is_relative_to(root_real)

    def _rewrite_virtual(self, text: str) -> str:
        """Replace virtual mount prefixes with physical paths (for bash commands)."""
        for prefix, phys in self.path_mappings.items():
            text = text.replace(prefix, str(phys))
        return text

    # -------------------------------------------------------------- file I/O
    def read_text(self, path: str) -> str:
        resolved = self.resolve(path, must_exist=True)
        if resolved.is_dir():
            raise IsADirectoryError(f"'{path}' is a directory, not a file.")
        if resolved.stat().st_size > _READ_MAX_BYTES:
            raise ValueError(
                f"'{path}' is larger than {_READ_MAX_BYTES} bytes; refuse to read as text."
            )
        return resolved.read_text(encoding="utf-8", errors="replace")

    def write_text(self, path: str, content: str, *, append: bool = False) -> Path:
        resolved = self.resolve(path)
        with self.file_lock(resolved):
            resolved.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with open(resolved, mode, encoding="utf-8") as fh:
                fh.write(content)
        return resolved

    def edit_text(self, path: str, old_str: str, new_str: str, *, replace_all: bool = False) -> int:
        """str_replace: unique-match read-modify-write under a single per-path lock.

        Returns the number of replacements. Raises if ``old_str`` is absent, or (when not
        ``replace_all``) occurs more than once.
        """
        resolved = self.resolve(path, must_exist=True)
        with self.file_lock(resolved):
            text = resolved.read_text(encoding="utf-8", errors="replace")
            count = text.count(old_str)
            if count == 0:
                raise ValueError(f"old_str not found in {path}.")
            if not replace_all and count > 1:
                raise ValueError(
                    f"old_str is not unique in {path} ({count} matches). Add surrounding "
                    f"context to make it unique, or pass replace_all=true."
                )
            resolved.write_text(text.replace(old_str, new_str), encoding="utf-8")
            return count

    # --------------------------------------------------------------- listing
    def list_dir(self, path: str, *, depth: int = 2, max_entries: int = 400) -> str:
        root = self.resolve(path, must_exist=True)
        if not root.is_dir():
            return f"{path} (file, {root.stat().st_size} bytes)"
        lines: list[str] = []
        base_depth = len(root.parts)
        for cur, dirs, files in os.walk(root):
            dirs.sort()
            cur_path = Path(cur)
            rel_depth = len(cur_path.parts) - base_depth
            if rel_depth >= depth:
                dirs[:] = []
            indent = "  " * rel_depth
            if cur_path != root:
                lines.append(f"{indent}{cur_path.name}/")
            for f in sorted(files):
                lines.append(f"{indent}{'  ' if cur_path != root else ''}{f}")
            if len(lines) > max_entries:
                lines.append(f"... (truncated at {max_entries} entries)")
                break
        return "\n".join(lines) if lines else "(empty)"

    # ----------------------------------------------------------------- glob
    def glob(
        self, pattern: str, path: str, *, include_dirs: bool = False, max_results: int = 100
    ) -> list[str]:
        if _has_traversal(pattern):
            raise PathEscapeError(f"glob pattern '{pattern}' may not be absolute or contain '..'.")
        root = self.resolve(path, must_exist=True)
        root_real = Path(os.path.realpath(root))
        matches = [
            p
            for p in root.glob(pattern)
            if (include_dirs or p.is_file()) and self._within(root_real, p)
        ]
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return [str(p) for p in matches[:max_results]]

    # ----------------------------------------------------------------- grep
    def grep(
        self,
        pattern: str,
        path: str,
        *,
        glob: str | None = None,
        literal: bool = False,
        case_sensitive: bool = False,
        max_results: int = 100,
    ) -> list[str]:
        if _has_traversal(glob):
            raise PathEscapeError(f"grep glob '{glob}' may not be absolute or contain '..'.")
        root = self.resolve(path, must_exist=True)
        root_real = Path(os.path.realpath(root))
        rg = shutil.which("rg")
        if rg:
            args = [rg, "--line-number", "--no-heading", "--color", "never", "-m", str(max_results)]
            if literal:
                args.append("--fixed-strings")
            if not case_sensitive:
                args.append("--ignore-case")
            if glob:
                args += ["--glob", glob]
            args += [pattern, str(root)]
            proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
            out = proc.stdout.splitlines()
            return out[:max_results]
        # Python fallback
        flags = 0 if case_sensitive else re.IGNORECASE
        rx = re.compile(re.escape(pattern) if literal else pattern, flags)
        results: list[str] = []
        for f in root.rglob(glob or "*"):
            if not f.is_file() or not self._within(root_real, f):
                continue
            try:
                for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        results.append(f"{f}:{i}:{line}")
                        if len(results) >= max_results:
                            return results
            except OSError:
                continue
        return results

    # ------------------------------------------------------------------ bash
    def run_bash(self, command: str, *, timeout: int = 120) -> str:
        if not self.bash_enabled:
            raise PermissionError("bash is disabled for this sandbox.")
        workspace = self.path_mappings[VIRTUAL_WORKSPACE]
        cmd = self._rewrite_virtual(command)
        env = _scrubbed_env()
        # start_new_session puts the shell in its own process group so a timeout can kill the
        # whole tree (grandchildren, backgrounded jobs), not just the shell.
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=str(workspace),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        out_chunks: list[str] = []
        err_chunks: list[str] = []
        # Drain both pipes in background so memory is bounded even for unbounded producers
        # (`yes`, `cat /dev/zero`): we keep reading to unblock the process but discard past the cap.
        t_out = threading.Thread(target=_bounded_drain, args=(proc.stdout, out_chunks), daemon=True)
        t_err = threading.Thread(target=_bounded_drain, args=(proc.stderr, err_chunks), daemon=True)
        t_out.start()
        t_err.start()
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_process_group(proc)
            proc.wait()
        t_out.join(timeout=2)
        t_err.join(timeout=2)
        out = "".join(out_chunks)
        err = "".join(err_chunks)
        parts = [
            f"[bash timed out after {timeout}s; process group killed]"
            if timed_out
            else f"[exit {proc.returncode}]"
        ]
        if out:
            parts.append(out.rstrip())
        if err:
            parts.append("[stderr]\n" + err.rstrip())
        return "\n".join(parts)


def _bounded_drain(stream: Any, sink: list[str], cap: int = _OUTPUT_CAP) -> None:
    """Read ``stream`` to EOF, keeping at most ``cap`` chars in ``sink`` (rest discarded)."""
    total = 0
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            if total < cap:
                sink.append(chunk[: cap - total])
            total += len(chunk)
    except (ValueError, OSError):  # stream closed mid-read (e.g. after killpg)
        pass


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


# Provider secrets always dropped; plus any env var whose name looks like a credential.
_EXPLICIT_SECRETS = frozenset(
    {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY", "DASHSCOPE_API_KEY"}
)
_SECRET_SUBSTRINGS = ("SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL")
_SECRET_SUFFIXES = ("_API_KEY", "_ACCESS_KEY", "_KEY")


def _is_secret_name(name: str) -> bool:
    n = name.upper()
    if n in _EXPLICIT_SECRETS:
        return True
    if any(s in n for s in _SECRET_SUBSTRINGS):
        return True
    return n.endswith(_SECRET_SUFFIXES)


def _scrubbed_env() -> dict[str, str]:
    """Copy the process env minus anything that looks like a secret, so bash can't read keys.

    bash has no filesystem confinement in the local provider, so this env scrub is the primary
    guard against a command exfiltrating credentials via ``env``/``printenv``.
    """
    return {k: v for k, v in os.environ.items() if not _is_secret_name(k)}


class LocalSandboxProvider:
    """Builds and caches one :class:`LocalSandbox` per thread."""

    def __init__(
        self,
        *,
        bash_enabled: bool = True,
        allowed_workspace_roots: list[Path] | None = None,
    ):
        self.bash_enabled = bash_enabled
        self.allowed_workspace_roots = allowed_workspace_roots
        self._cache: dict[str, LocalSandbox] = {}
        self._guard = threading.Lock()

    def _check_external_workspace(self, tp: ThreadPaths) -> None:
        if not tp.workspace_is_external:
            return
        if not tp.workspace.exists():
            raise FileNotFoundError(f"Existing workspace does not exist: {tp.workspace}")
        if self.allowed_workspace_roots:
            ws_real = Path(os.path.realpath(tp.workspace))
            roots_real = [Path(os.path.realpath(r)) for r in self.allowed_workspace_roots]
            ok = any(ws_real == r or ws_real.is_relative_to(r) for r in roots_real)
            if not ok:
                raise PathEscapeError(
                    f"Workspace {tp.workspace} is not under an allowed root "
                    f"({', '.join(map(str, self.allowed_workspace_roots))})."
                )

    def acquire(self, tp: ThreadPaths) -> LocalSandbox:
        with self._guard:
            existing = self._cache.get(tp.thread_id)
            if existing is not None:
                return existing
            self._check_external_workspace(tp)
            tp.ensure()
            sandbox = LocalSandbox(
                sandbox_id=f"local:{tp.thread_id}",
                path_mappings=tp.virtual_map(),
                bash_enabled=self.bash_enabled,
            )
            self._cache[tp.thread_id] = sandbox
            return sandbox

    def release(self, thread_id: str) -> None:
        with self._guard:
            self._cache.pop(thread_id, None)
