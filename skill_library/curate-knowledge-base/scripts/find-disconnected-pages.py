#!/usr/bin/env python3
"""Find the disconnected *islands* in a Logseq DB graph.

Unlike the Obsidian original, this script does NOT read files or parse
wikilinks: the Logseq DB already knows the link graph. The curator supplies
two JSON inputs, both straight from `logseq query --output json`:

  --pages-json  result rows of the user-page query, e.g. [["Alpha",2], ...]
                (title is row[0]; any extra columns are ignored)
  --edges-json  result rows of the page->page ref-edge query, e.g.
                [["Alpha","Beta"], ["Beta","Gamma"], ...]  (directed)

Node ids are page TITLES (the DB is the id authority). Connectivity is
undirected (a link either way relates two pages); orphans/dead-ends are
reported from the directed edges. Pure stdlib; no graph I/O of its own.

Usage:
    logseq query ... --query '<pages>' --output json > pages.json
    logseq query ... --query '<edges>' --output json > edges.json
    python3 find-disconnected-pages.py --pages-json pages.json --edges-json edges.json --json
    # or stream: ... --pages-json - --edges-json edges.json   (pages on stdin)
"""

from __future__ import annotations

import argparse
import json
import sys


def _extract_rows(payload) -> list:
    """Accept either a raw `logseq query` envelope or a bare list of rows."""
    if isinstance(payload, dict):
        return (payload.get("data") or {}).get("result") or []
    return payload or []


def _pages_from_rows(rows) -> list[str]:
    """First column of each row is the page title (extra columns ignored)."""
    out: list[str] = []
    for row in rows:
        title = row[0] if isinstance(row, (list, tuple)) else row
        if title is not None:
            out.append(str(title))
    return out


def _edges_from_rows(rows) -> list[tuple[str, str]]:
    edges: list[tuple[str, str]] = []
    for row in rows:
        if isinstance(row, (list, tuple)) and len(row) >= 2 and row[0] is not None and row[1] is not None:
            edges.append((str(row[0]), str(row[1])))
    return edges


def _components(nodes: list[str], adjacency: dict[str, set[str]]) -> list[list[str]]:
    seen: set[str] = set()
    components: list[list[str]] = []
    for start in nodes:  # nodes is pre-sorted → deterministic order
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


def analyze(pages: list[str], edges: list[tuple[str, str]]) -> dict:
    """Connectivity report for a Logseq graph. See module docstring for shape."""
    # Every page is a node even with no edges (its own singleton component).
    # Include edge endpoints too, defensively, in case a ref names a page the
    # user-page query filtered out.
    nodes = sorted({*pages, *(a for a, _ in edges), *(b for _, b in edges)})
    adjacency: dict[str, set[str]] = {n: set() for n in nodes}
    inbound: dict[str, int] = {n: 0 for n in nodes}
    outbound: dict[str, int] = {n: 0 for n in nodes}
    edge_pairs: set[frozenset[str]] = set()
    for a, b in edges:
        if a == b:
            continue
        adjacency[a].add(b)
        adjacency[b].add(a)
        outbound[a] += 1
        inbound[b] += 1
        edge_pairs.add(frozenset((a, b)))

    components = _components(nodes, adjacency)
    note_count = len(nodes)
    edge_count = len(edge_pairs)

    main = components[0] if components else []
    if len(main) >= 2:
        rest = components[1:]
        islands = [{"size": len(c), "members": c} for c in rest if len(c) >= 2]
        isolated = [c[0] for c in rest if len(c) == 1]
        main_component = {"size": len(main), "members": main}
    else:
        islands = []
        isolated = [c[0] for c in components if len(c) == 1]
        main_component = {"size": 0, "members": []}

    orphans = sorted(n for n in pages if inbound.get(n, 0) == 0)
    deadends = sorted(n for n in pages if outbound.get(n, 0) == 0)

    return {
        "note_count": note_count,
        "edge_count": edge_count,
        "edges_per_note": round(edge_count / note_count, 3) if note_count else 0.0,
        "component_count": len(components),
        "main_component": main_component,
        "islands": islands,
        "isolated": isolated,
        "orphans": orphans,
        "deadends": deadends,
    }


def _read_json_arg(value: str):
    if value == "-":
        return json.load(sys.stdin)
    with open(value, encoding="utf-8") as fh:
        return json.load(fh)


def _format_human(report: dict) -> str:
    lines = [
        f"Notes: {report['note_count']} | Edges: {report['edge_count']} | "
        f"Edges/note: {report['edges_per_note']} | Components: {report['component_count']}",
        f"Main graph: {report['main_component']['size']} pages",
        f"Islands (disconnected clusters of >=2 pages): {len(report['islands'])}",
    ]
    for i, island in enumerate(report["islands"], 1):
        lines.append(f"  [{i}] size {island['size']}: {', '.join(island['members'])}")
    lines.append(f"Isolated pages (no resolved links): {len(report['isolated'])}")
    for name in report["isolated"]:
        lines.append(f"  - {name}")
    lines.append(f"Orphans (no inbound): {len(report['orphans'])} | Dead-ends (no outbound): {len(report['deadends'])}")
    if report["component_count"] == 1 and not report["islands"] and not report["isolated"]:
        lines.append("Graph is a single connected knowledge graph — no islands to reconcile.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find disconnected islands in a Logseq DB graph.")
    parser.add_argument("--pages-json", required=True, help="Path to user-page query JSON, or '-' for stdin.")
    parser.add_argument("--edges-json", required=True, help="Path to page-edge query JSON, or '-' for stdin.")
    parser.add_argument("--json", action="store_true", help="Emit the machine-readable JSON report.")
    args = parser.parse_args(argv)

    pages = _pages_from_rows(_extract_rows(_read_json_arg(args.pages_json)))
    edges = _edges_from_rows(_extract_rows(_read_json_arg(args.edges_json)))
    report = analyze(pages, edges)
    print(json.dumps(report, indent=2) if args.json else _format_human(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
