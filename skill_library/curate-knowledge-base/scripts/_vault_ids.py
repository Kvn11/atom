"""Shared vault id / path helpers for the obsidian-lint scripts.

`find-disconnected-notes.py` and `find-recently-modified-notes.py` MUST agree on
node ids and on which files count as vault notes — the curator intersects their
outputs (changed ids ∩ connected-component members) to scope an incremental
pass, and any divergence silently produces the empty set. Keeping the contract
in ONE stdlib-only module (imported as a sibling when a script is run directly,
since `python3 path/script.py` puts the script's dir on `sys.path`) makes drift
structurally impossible.

A node id is the note's vault-relative path WITHOUT the `.md` suffix, posix-style
(`sub/Beta`). This matches `kiwi.vault.graph` so the ids also line up with the
run-page vault graph.
"""

from __future__ import annotations

from pathlib import Path


def is_hidden(rel: Path) -> bool:
    """True if any path component is a dotfile/dotdir (e.g. `.obsidian/`)."""
    return any(part.startswith(".") for part in rel.parts)


def node_id(vault_path: Path, file: Path) -> str:
    """Node id for a file on disk: vault-relative path without `.md`, posix-style."""
    return file.relative_to(vault_path).with_suffix("").as_posix()


def rel_to_node_id(rel: str) -> str:
    """Node id from an ALREADY-vault-relative path string (e.g. a git path)."""
    return Path(rel).with_suffix("").as_posix()


def collect_md_files(vault_path: Path) -> list[Path]:
    """Sorted list of non-hidden `.md` files under the vault.

    Sorted for determinism — also makes basename-collision resolution stable
    (first sorted file wins a bare `[[Name]]`), matching kiwi.vault.graph.
    """
    files = [p for p in vault_path.rglob("*.md") if p.is_file() and not is_hidden(p.relative_to(vault_path))]
    return sorted(files)
