#!/usr/bin/env python3
"""List the notes in an Obsidian vault changed since a reference point.

Companion to find-disconnected-notes.py: the curator runs this to scope an
incremental pass, then intersects the changed-note ids with that script's
connected components to decide which groups to re-digest. Node ids are
relpath-without-extension (posix), identical to find-disconnected-notes.py.

Two modes (exactly one required):
  --since <ISO-8601 | epoch-seconds>   filesystem mtime mode
  --since-git <ref>                    git mode (changed .md vs <ref>, for cloned KBs)

Usage:
    python3 find-recently-modified-notes.py /mnt/user-data/<vault> --since 2026-05-01
    python3 find-recently-modified-notes.py /path/to/cloned-kb --since-git origin/main --json
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from _vault_ids import collect_md_files, is_hidden, node_id, rel_to_node_id


def _parse_since(since: str) -> float:
    """Accept epoch seconds or an ISO-8601 datetime; return epoch seconds.

    A naive ISO datetime (no offset) is interpreted as UTC, not local time, so
    `--since 2026-05-01` means the same instant regardless of the host timezone.
    """
    try:
        return float(since)
    except ValueError:
        pass
    dt = datetime.fromisoformat(since)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _mtime_changed(vault_path: Path, since: str) -> list[str]:
    threshold = _parse_since(since)
    ids = [node_id(vault_path, p) for p in collect_md_files(vault_path) if p.stat().st_mtime >= threshold]
    return sorted(ids)


def _git_changed(vault_path: Path, ref: str) -> list[str]:
    # `git diff --name-only` prints paths relative to the REPO ROOT, not to
    # vault_path. If the vault is a subdirectory of the repo, rebase those paths
    # onto the vault so node ids match find-disconnected-notes.py (which is
    # vault-relative); otherwise the curator's id-intersection silently misses
    # every note. `--show-prefix` is the vault's path within the repo ("kb/" or
    # "" at the root). `core.quotePath=false` keeps non-ASCII names un-escaped.
    prefix_proc = subprocess.run(
        ["git", "-C", str(vault_path), "rev-parse", "--show-prefix"],
        capture_output=True,
        text=True,
    )
    if prefix_proc.returncode != 0:
        raise RuntimeError(f"git failed (not a repo at '{vault_path}'?): {prefix_proc.stderr.strip()}")
    prefix = prefix_proc.stdout.strip()  # e.g. "kb/" or "" when the vault is the repo root

    proc = subprocess.run(
        ["git", "-c", "core.quotePath=false", "-C", str(vault_path), "diff", "--name-only", ref, "--", "."],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git diff failed (bad ref '{ref}'?): {proc.stderr.strip()}")
    ids = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if prefix:
            if not line.startswith(prefix):
                continue  # changed file outside the vault subtree
            rel = line[len(prefix) :]
        else:
            rel = line
        if not rel.endswith(".md") or is_hidden(Path(rel)):
            continue
        ids.append(rel_to_node_id(rel))
    return sorted(set(ids))  # set(): defensive; git diff --name-only is already unique


def list_recent(vault_path: Path | str, since: str | None = None, since_git: str | None = None) -> dict:
    """Return {vault, mode, since, changed:[node_id,...]} for notes changed since the reference."""
    vault_path = Path(vault_path)
    if not since and not since_git:
        raise ValueError("list_recent requires a non-empty one of: since, since_git")
    if since_git is not None:
        changed = _git_changed(vault_path, since_git) if vault_path.is_dir() else []
        return {"vault": str(vault_path), "mode": "git", "since": since_git, "changed": changed}
    changed = _mtime_changed(vault_path, since) if vault_path.is_dir() else []
    return {"vault": str(vault_path), "mode": "mtime", "since": since, "changed": changed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="List notes changed since a reference point.")
    parser.add_argument("vault_path", help="Path to the vault, e.g. /mnt/user-data/<vault>")
    parser.add_argument("--since", help="ISO-8601 datetime or epoch seconds (filesystem mtime mode)")
    parser.add_argument("--since-git", dest="since_git", help="git ref; reports changed .md vs this ref")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args(argv)

    if not args.since and not args.since_git:
        parser.error("one of --since or --since-git is required")
    try:
        report = list_recent(args.vault_path, since=args.since, since_git=args.since_git)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"Vault: {report['vault']}")
            print(f"Mode: {report['mode']} | since: {report['since']} | changed notes: {len(report['changed'])}")
            for nid in report["changed"]:
                print(f"  - {nid}")
    except RuntimeError as exc:
        parser.exit(status=2, message=f"error: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
