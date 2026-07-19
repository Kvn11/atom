#!/usr/bin/env python3
"""Find the disconnected *islands* in an Obsidian vault.

The `obsidian` CLI exposes `orphans` (notes with 0 inbound links) and `deadends`
(0 outbound links), but neither can see a multi-note *island* — a cluster of
notes that wikilink among THEMSELVES yet never connect to the main graph. Such
an island has no orphans and no deadends, so it is invisible to the CLI. This
script fills that gap: it parses the vault's `[[wikilinks]]`, builds the
*undirected* link graph, and reports its connected components so a reviewer can
see which clusters are stranded.

Why undirected: a link in either direction means the two notes are related, so
for connectivity `A -> B` joins A and B regardless of a return link.

Wikilink parsing + resolution is ported VERBATIM from
`backend/packages/harness/kiwi/vault/graph.py` so edge semantics match the
run-page vault graph exactly (case-insensitive resolution, code spans ignored,
embeds dropped, unresolved + self links dropped). The script is intentionally
stdlib-only and self-contained — the sandbox cannot import the backend package.

Usage:
    python3 find-disconnected-notes.py /mnt/user-data/<vault-name>          # human summary
    python3 find-disconnected-notes.py /mnt/user-data/<vault-name> --json   # machine-readable
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from _vault_ids import collect_md_files, node_id

# ── Wikilink parsing (ported verbatim from kiwi.vault.graph) ────────────────
# Match `[[Target]]`, `[[Target#Section]]`, `[[Target|Alias]]`, and the combined
# `[[Target#Section|Alias]]`. The leading negative lookbehind drops embeds
# (`![[...]]`). Group 1 captures only the page target. `\n` is excluded from
# every component so a stray `[[` and a later `]]` on another line can't be read
# as one (multi-line) link.
_WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\]|#\n]+)(?:#[^\]|\n]+)?(?:\|[^\]\n]+)?\]\]")

# Code spans are not link sources in Obsidian's graph, so strip fenced (``` / ~~~)
# and inline (`...`) code before scanning, mirroring Obsidian's own behavior.
_FENCED_CODE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def parse_wikilinks(text: str) -> list[str]:
    """Return the page targets of every (non-embed) wikilink, in source order."""
    stripped = _INLINE_CODE_RE.sub("", _FENCED_CODE_RE.sub("", text))
    return [m.strip() for m in _WIKILINK_RE.findall(stripped) if m.strip()]


# ── Vault walking + graph construction ──────────────────────────────────────
# Node-id / hidden / file-collection helpers live in `_vault_ids` so this script
# and find-recently-modified-notes.py cannot drift (the curator intersects their
# outputs). Wikilink parsing + resolution below are graph-specific and local.


def _build_undirected_adjacency(vault_path: Path) -> tuple[list[str], dict[str, set[str]]]:
    """Walk the vault and return (sorted node ids, undirected adjacency).

    Edges resolve exactly as kiwi.vault.graph does: case-insensitive by relpath
    then basename, with unresolved targets and self-links dropped. Every note is
    a node even if it has no edges (it becomes its own singleton component).
    """
    files = collect_md_files(vault_path)

    by_relpath: dict[str, str] = {}
    by_basename: dict[str, str] = {}
    node_ids: list[str] = []
    for file in files:
        nid = node_id(vault_path, file)
        node_ids.append(nid)
        by_relpath[nid.lower()] = nid
        by_basename.setdefault(file.stem.lower(), nid)

    def _resolve(target: str) -> str | None:
        key = target.strip().lower()
        return by_relpath.get(key) or by_basename.get(key)

    adjacency: dict[str, set[str]] = {nid: set() for nid in node_ids}
    for file in files:
        try:
            content = file.read_text(encoding="utf-8")
        except OSError:
            continue
        source = node_id(vault_path, file)
        for raw_target in parse_wikilinks(content):
            target = _resolve(raw_target)
            if target is None or target == source:
                continue
            adjacency[source].add(target)
            adjacency[target].add(source)
    return node_ids, adjacency


def _count_edges(adjacency: dict[str, set[str]]) -> int:
    return sum(len(neighbors) for neighbors in adjacency.values()) // 2


# ── Connected components ─────────────────────────────────────────────────────


def connected_components(vault_path: Path) -> list[list[str]]:
    """Return the undirected wikilink graph's connected components.

    Each component is a sorted list of node ids; the component list is sorted by
    (descending size, first member) so the largest component (the "main graph")
    is first and the ordering is deterministic.
    """
    node_ids, adjacency = _build_undirected_adjacency(Path(vault_path))
    return _components_from_adjacency(node_ids, adjacency)


def _components_from_adjacency(node_ids: list[str], adjacency: dict[str, set[str]]) -> list[list[str]]:
    seen: set[str] = set()
    components: list[list[str]] = []
    for start in node_ids:  # node_ids is already sorted → deterministic BFS order
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        members: list[str] = []
        while stack:
            node = stack.pop()
            members.append(node)
            for neighbor in adjacency[node]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        components.append(sorted(members))
    components.sort(key=lambda c: (-len(c), c[0]))
    return components


# ── Report ───────────────────────────────────────────────────────────────────


def analyze_vault(vault_path: Path | str) -> dict:
    """Build the connectivity report for a vault.

    Shape:
        note_count, edge_count, edges_per_note, component_count,
        main_component: {size, members}   # the largest component, or {0, []} when
                                          # no component has >= 2 notes (no main graph)
        islands:  [{size, members}, ...]  # non-main components with >= 2 notes
        isolated: [node_id, ...]          # singleton notes (no resolved links)
    """
    vault_path = Path(vault_path)
    if not vault_path.is_dir():
        return {
            "vault": str(vault_path),
            "note_count": 0,
            "edge_count": 0,
            "edges_per_note": 0.0,
            "component_count": 0,
            "main_component": {"size": 0, "members": []},
            "islands": [],
            "isolated": [],
        }

    node_ids, adjacency = _build_undirected_adjacency(vault_path)
    components = _components_from_adjacency(node_ids, adjacency)
    note_count = len(node_ids)
    edge_count = _count_edges(adjacency)

    main = components[0] if components else []
    if len(main) >= 2:
        # A real multi-note main graph. Islands = other components with >= 2 notes;
        # isolated = the remaining singletons.
        rest = components[1:]
        islands = [{"size": len(c), "members": c} for c in rest if len(c) >= 2]
        isolated = [c[0] for c in rest if len(c) == 1]
        main_component = {"size": len(main), "members": main}
    else:
        # No component has >= 2 notes → there is no main graph. EVERY note is a
        # singleton and must surface as isolated (don't silently drop the largest
        # one into main_component).
        islands = []
        isolated = [c[0] for c in components if len(c) == 1]
        main_component = {"size": 0, "members": []}

    return {
        "vault": str(vault_path),
        "note_count": note_count,
        "edge_count": edge_count,
        "edges_per_note": round(edge_count / note_count, 3) if note_count else 0.0,
        "component_count": len(components),
        "main_component": main_component,
        "islands": islands,
        "isolated": isolated,
    }


def _format_human(report: dict) -> str:
    lines: list[str] = []
    lines.append(f"Vault: {report['vault']}")
    lines.append(f"Notes: {report['note_count']} | Edges: {report['edge_count']} | Edges/note: {report['edges_per_note']} | Components: {report['component_count']}")
    lines.append(f"Main graph: {report['main_component']['size']} notes")

    islands = report["islands"]
    lines.append(f"Islands (disconnected clusters of >=2 notes): {len(islands)}")
    for i, island in enumerate(islands, 1):
        members = ", ".join(island["members"])
        lines.append(f"  [{i}] size {island['size']}: {members}")

    isolated = report["isolated"]
    lines.append(f"Isolated notes (no resolved links): {len(isolated)}")
    for name in isolated:
        lines.append(f"  - {name}")

    if not islands and not isolated and report["component_count"] == 1:
        lines.append("Vault is a single connected knowledge graph — no islands to reconcile.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find disconnected islands in an Obsidian vault.")
    parser.add_argument("vault_path", help="Filesystem path to the vault, e.g. /mnt/user-data/<vault-name>")
    parser.add_argument("--json", action="store_true", help="Emit the machine-readable JSON report.")
    args = parser.parse_args(argv)

    report = analyze_vault(args.vault_path)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(_format_human(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
