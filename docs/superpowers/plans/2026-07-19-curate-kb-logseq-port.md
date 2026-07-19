# Curate-Knowledge-Base Logseq Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the `curate-knowledge-base` skill in place from Obsidian (`.md` file-walking) to atom's Logseq DB backend, preserving the full map-reduce curation methodology while re-grounding only the substrate (Sense, read, annotate, change-detection) on the `logseq` CLI.

**Architecture:** One skill, same name/dir. `SKILL.md` keeps its 12-section structure; substrate sections (§1, §3, §8, §9, §11) are rewritten, others get vocabulary edits. Island detection moves from a `.md`-walking Python script to a native `logseq query` page→page edge list fed to a slimmed, pure-stdlib component-finder. Flags become queryable `#curator` blocks instead of `> [!curator]` callouts. Two of the three bundled scripts are deleted.

**Tech Stack:** Python 3.12 (stdlib only, for the one remaining script + its pytest), the `logseq` CLI (Datascript DB graph), Markdown (the SKILL.md prose).

## Global Constraints

- **Provider is Logseq-only.** No Obsidian, no clone, no rename, no dual-vault. The skill takes its graph from its prompt; it does NOT hard-wire atom's `$ATOM_HOME/notes/<slug>` layout.
- **Every `logseq` invocation** includes `--graph <NAME>` and, when a `root_dir` prompt param is given, `--root-dir <PATH>`. Add `--output json` for any command the curator parses.
- **Never hardcode `:user.property/*` or `:user.class/*` db/idents in any Datascript query.** They carry a random per-graph suffix (e.g. `user.property/curator-type-xaesFYyu`). Read queries key off tag `:block/name` and `:block/refs`; writes use friendly property names (`:curator-type`), which the CLI resolves.
- **`since_git` incremental mode is removed** (a DB graph is not markdown-under-git). `since` is interpreted against `:block/updated-at`.
- **Curator remains the single writer.** Earned-edits-only; flags are written but contradictions/stale/ambiguous claims are NEVER resolved; dual-channel surfacing (in-graph `#curator` flag + caller report); no activity/skip/coverage logs in the graph.
- **Pure-stdlib scripts.** The one remaining script imports nothing outside the Python stdlib and does no graph I/O itself (the curator runs `logseq query` and pipes JSON in).
- **The methodology text (§2, §4, §5, §6, §7, §10, §12) is preserved**; only substrate commands, the `[!curator]` format, and vault/note vocabulary change.

---

## File Structure

- `skill_library/curate-knowledge-base/SKILL.md` — **modify.** Frontmatter description + §1, §3, §8, §9, §11 rewritten; §2/§4/§5/§6/§7/§10/§12 vocabulary edits (vault→graph, note→page, `[!curator]`→`#curator`).
- `skill_library/curate-knowledge-base/scripts/find-disconnected-pages.py` — **create.** Pure component-finder: `analyze(pages, edges) -> dict`; reads `--pages-json` + `--edges-json` (file paths or `-` for stdin); emits the same report JSON shape as the old script.
- `skill_library/curate-knowledge-base/scripts/find-disconnected-notes.py` — **delete.**
- `skill_library/curate-knowledge-base/scripts/find-recently-modified-notes.py` — **delete.**
- `skill_library/curate-knowledge-base/scripts/_vault_ids.py` — **delete.**
- `tests/test_curate_disconnected_pages.py` — **create.** Pure unit test of `find-disconnected-pages.py` (imports the script via `importlib`; no `logseq` CLI needed, so it runs in CI).

**Not a committed test (CI has no `logseq` CLI):** the fixture-graph smoke test in Task 5 is a *manual local* verification, run by the implementer with the `logseq` CLI. Its commands + expected output are given verbatim.

---

## Verified command reference (empirically confirmed against a scratch graph)

Every command below was run and its output confirmed during design. Reuse these verbatim.

**Graph existence (§1):**
```bash
logseq graph list --output json                 # {"data":{"graphs":[...]}}
logseq graph info --graph <NAME> [--root-dir <R>] --output json
```

**Real user pages — non-built-in, non-journal, ≥1 block (§3 "files"):**
```bash
logseq query --graph <NAME> [--root-dir <R>] --output json --query \
'[:find ?t (count ?b) :where [?p :block/name] [?p :block/title ?t] (not [?p :db/ident]) (not [?p :block/journal-day]) [?b :block/page ?p]]'
```
Returns `[["Alpha",2],["Beta",1],…]` — excludes `logseq.class/*` built-ins AND system pages (`$$$views`, `Recycle`, `Contents`, `Library`, `Quick add`, `$$$favorites`).

**Page→page reference edges (§3 islands input):**
```bash
logseq query --graph <NAME> [--root-dir <R>] --output json --query \
'[:find ?fp ?tp :where [?b :block/page ?f] [?f :block/title ?fp] [?b :block/refs ?t] [?t :block/title ?tp] [?t :block/name]]'
```
Returns `[["Gamma","Alpha"],["Alpha","Beta"],…]` — tags & built-ins excluded.

**Recently changed (§3 incremental):**
```bash
logseq list node --graph <NAME> [--root-dir <R>] --sort updated-at --order desc --output json
```

**Tag / property stats (§3):**
```bash
logseq list tag      --graph <NAME> [--root-dir <R>] --output json
logseq list property --graph <NAME> [--root-dir <R>] --output json
```

**Read one page + block tree + backlinks (§11 worker read):**
```bash
logseq show --graph <NAME> [--root-dir <R>] --page "<Page>" --linked-references true
```
Renders each block with its numeric id and a "Linked References" section.

**Schema bootstrap — run ONCE before the first flag (§8):**
```bash
logseq upsert tag      --graph <NAME> [--root-dir <R>] --name curator
logseq upsert property --graph <NAME> [--root-dir <R>] --name curator-type           --type default
logseq upsert property --graph <NAME> [--root-dir <R>] --name curator-flagged        --type default
logseq upsert property --graph <NAME> [--root-dir <R>] --name curator-conflicts-with --type default
```
(All three properties are `type default` / text. `type date` rejects a plain `YYYY-MM-DD` string — it demands a journal date — and leaves a partial write, so use `default`.)

**Write a flag block, block-attached when a block id is known (§8/§9):**
```bash
logseq upsert block --graph <NAME> [--root-dir <R>] --target-page "<HostPage>" \
  [--target-id <blockId> --pos last-child] \
  --content 'Contradiction: claim "X" here conflicts with [[Other]]; resolving needs domain analysis. — wiki-curator' \
  --update-tags '["curator"]' \
  --update-properties '{:curator-type "contradiction" :curator-flagged "<YYYY-MM-DD>" :curator-conflicts-with "Other"}'
```
Confirmed to render the block with `#curator` and the three properties. Omit `--target-id`/`--pos` to attach at page level.

**Enumerate open flags — PORTABLE idempotency query, no hardcoded idents (§9):**
```bash
logseq query --graph <NAME> [--root-dir <R>] --output json --query \
'[:find ?host ?content :where [?t :block/name "curator"] [?b :block/tags ?t] [?b :block/page ?hp] [?hp :block/title ?host] [?b :block/title ?content]]'
```
For contradiction/stale flags, also get the conflicting-page ref (exclude the curator tag itself):
```bash
logseq query --graph <NAME> [--root-dir <R>] --output json --query \
'[:find ?host ?refname ?content :where [?t :block/name "curator"] [?b :block/tags ?t] [?b :block/page ?hp] [?hp :block/title ?host] [?b :block/title ?content] [?b :block/refs ?r] [(not= ?r ?t)] [?r :block/name] [?r :block/title ?refname]]'
```
Returns e.g. `[["Gamma","Alpha","Stale claim: … [[Alpha]] …"]]`.

**Remove a resolved flag — convergence (§9):**
```bash
logseq remove block --graph <NAME> [--root-dir <R>] --id <blockId>     # or --uuid <uuid>
```

---

## Task 1: Baseline commit of the imported Obsidian skill

Commit the skill exactly as received (it is currently untracked: `?? skill_library/`) so the port is a legible diff. No code change.

**Files:**
- Add: `skill_library/curate-knowledge-base/` (all four files, as-is)

- [ ] **Step 1: Confirm the branch and the untracked skill**

Run: `git branch --show-current && git status --porcelain skill_library`
Expected: branch `feat/curate-kb-logseq-port`; output lists `?? skill_library/`.

- [ ] **Step 2: Stage and commit the baseline**

```bash
git add skill_library/curate-knowledge-base
git commit -m "chore(skill): vendor curate-knowledge-base (Obsidian) as port baseline

Imported as-received from the kiwi harness; the next commits port it to
atom's Logseq DB backend. Committed unmodified so the port is a clean diff."
```

- [ ] **Step 3: Verify the baseline landed**

Run: `git show --stat HEAD | head -20`
Expected: the 4 files (`SKILL.md`, `scripts/_vault_ids.py`, `scripts/find-disconnected-notes.py`, `scripts/find-recently-modified-notes.py`) added.

---

## Task 2: New `find-disconnected-pages.py` component-finder (TDD)

A pure-stdlib analyzer that takes a page list + a directed page→page edge list (both from `logseq query`) and reports connected components (islands + isolated), plus directed orphans/dead-ends. Reuses the BFS from the old script; drops all file-walking, wikilink regex, and `kiwi.vault.graph` porting. Node ids are **page titles** (from the DB), not relpaths.

**Files:**
- Create: `skill_library/curate-knowledge-base/scripts/find-disconnected-pages.py`
- Test: `tests/test_curate_disconnected_pages.py`

**Interfaces:**
- Produces: `analyze(pages: list[str], edges: list[tuple[str, str]]) -> dict` with keys `note_count, edge_count, edges_per_note, component_count, main_component:{size,members}, islands:[{size,members}], isolated:[str], orphans:[str], deadends:[str]`. Edges are directed `(from_title, to_title)`; connectivity is computed undirected. `orphans` = pages with no inbound directed edge; `deadends` = pages with no outbound directed edge (both restricted to pages present in `pages`).
- CLI: `find-disconnected-pages.py --pages-json <path|-> --edges-json <path|-> [--json]`. Each JSON input is the raw `{"data":{"result":[…]}}` from `logseq query` OR a bare list (`analyze` is fed the extracted `result`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_curate_disconnected_pages.py`:

```python
import importlib.util
import json
import pathlib
import subprocess
import sys

SCRIPT = (
    pathlib.Path(__file__).resolve().parent.parent
    / "skill_library/curate-knowledge-base/scripts/find-disconnected-pages.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("find_disconnected_pages", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Fixture mirrors the design-time probe graph: a 3-page cycle (main graph),
# a 2-page island, and one link-less orphan page.
PAGES = ["Alpha", "Beta", "Gamma", "IslandX", "IslandY", "Lonely"]
EDGES = [
    ("Alpha", "Beta"), ("Beta", "Gamma"), ("Gamma", "Alpha"),
    ("IslandX", "IslandY"), ("IslandY", "IslandX"),
]


def test_analyze_partitions_main_island_and_isolated():
    mod = _load()
    report = mod.analyze(PAGES, EDGES)
    assert report["note_count"] == 6
    assert report["component_count"] == 3
    assert report["main_component"]["size"] == 3
    assert set(report["main_component"]["members"]) == {"Alpha", "Beta", "Gamma"}
    assert report["islands"] == [{"size": 2, "members": ["IslandX", "IslandY"]}]
    assert report["isolated"] == ["Lonely"]


def test_analyze_orphans_and_deadends_are_directed():
    mod = _load()
    report = mod.analyze(PAGES, EDGES)
    # Every page in the two cycles has both an inbound and an outbound edge;
    # only Lonely (no edges at all) is both an orphan and a dead-end.
    assert report["orphans"] == ["Lonely"]
    assert report["deadends"] == ["Lonely"]


def test_analyze_empty_graph():
    mod = _load()
    report = mod.analyze([], [])
    assert report["note_count"] == 0
    assert report["component_count"] == 0
    assert report["main_component"] == {"size": 0, "members": []}
    assert report["islands"] == []
    assert report["isolated"] == []


def test_cli_reads_logseq_query_result_shape(tmp_path):
    # Accepts the raw `logseq query --output json` envelope, not just bare lists.
    pages_file = tmp_path / "pages.json"
    edges_file = tmp_path / "edges.json"
    pages_file.write_text(json.dumps({"data": {"result": [[p, 1] for p in PAGES]}}))
    edges_file.write_text(json.dumps({"data": {"result": [list(e) for e in EDGES]}}))
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--pages-json", str(pages_file),
         "--edges-json", str(edges_file), "--json"],
        capture_output=True, text=True, check=True,
    )
    report = json.loads(out.stdout)
    assert report["component_count"] == 3
    assert report["isolated"] == ["Lonely"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /Users/kev/gitclones/atom && .venv/bin/python -m pytest tests/test_curate_disconnected_pages.py -v`
Expected: FAIL — the script file does not exist yet (collection error / `FileNotFoundError` in `_load`).

- [ ] **Step 3: Write the script**

Create `skill_library/curate-knowledge-base/scripts/find-disconnected-pages.py`:

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd /Users/kev/gitclones/atom && .venv/bin/python -m pytest tests/test_curate_disconnected_pages.py -v`
Expected: PASS — all 4 tests green.

- [ ] **Step 5: Commit**

```bash
git add skill_library/curate-knowledge-base/scripts/find-disconnected-pages.py tests/test_curate_disconnected_pages.py
git commit -m "feat(skill): Logseq component-finder fed by logseq query edges

Pure-stdlib analyze(pages, edges): undirected components (islands +
isolated) + directed orphans/dead-ends. Node ids are page titles; no
file-walking or wikilink regex. Unit-tested without the logseq CLI."
```

---

## Task 3: Rewrite the SKILL.md substrate

Rewrite the frontmatter description and §1, §3, §8, §9, §11; apply vocabulary edits (vault→graph, note→page, `> [!curator]`→`#curator` block) to §2, §4, §5, §6, §7, §10, §12. All exact commands/formats are in the "Verified command reference" above — embed them verbatim.

**Files:**
- Modify: `skill_library/curate-knowledge-base/SKILL.md`

- [ ] **Step 1: Rewrite the frontmatter `description`**

Replace "clean, organize, and connect an **Obsidian** knowledge base" with "clean, organize, and connect a **Logseq** knowledge base (DB graph, via the `logseq` CLI)". Keep the rest of the description (map-reduce, senses/partitions/fans-out/reduces, FLAGS-never-resolves, `curate-<domain>` lens, never writes operational logs). Change the closing invariant "Never writes operational logs to the vault." → "…to the graph."

- [ ] **Step 2: Rewrite §1 — Preconditions & graph**

Replace the vault prose with:
- Unit of work is a Logseq **graph**, named in the prompt as `graph=<NAME>` (optionally `root_dir=<PATH>` when not under the CLI default `~/logseq`). **No default graph** — if none is named, STOP and return a report; never guess.
- Confirm the graph exists: `logseq graph list --output json` (and/or `logseq graph info --graph <NAME> [--root-dir <R>] --output json`). If absent/errors, STOP and report that graph `<NAME>` was not found; graph acquisition is the caller's responsibility.
- State the invariant that **every** subsequent `logseq` call carries `--graph <NAME>` (and `--root-dir <PATH>` when `root_dir` was given).
- Optional params: `domain=<domain>`, `since=<ISO|epoch>` (against `:block/updated-at`), `full`, `max_passes=<N>`. **Remove `since_git`.**

- [ ] **Step 3: Rewrite §3 — Sense**

Replace the "Always run these commands" block with the Sense command mapping from the Verified command reference (real user pages query, page→page edge query, `list tag`, `list property`, and the derivation note that orphans/dead-ends now come from `find-disconnected-pages.py`'s `orphans`/`deadends` output). The island command becomes:
```bash
logseq query --graph <NAME> [--root-dir <R>] --output json --query '<user-pages query>' > /tmp/pages.json
logseq query --graph <NAME> [--root-dir <R>] --output json --query '<page-edges query>' > /tmp/edges.json
python3 <skill_dir>/scripts/find-disconnected-pages.py --pages-json /tmp/pages.json --edges-json /tmp/edges.json --json
```
Keep the "Record from this output" list but map fields to the new report keys (`component_count`, `main_component`, `islands`, `isolated`, `orphans`, `deadends`). For the incremental block, replace both `find-recently-modified-notes.py` invocations with `logseq list node --sort updated-at --order desc --output json` and the intersection prose (changed page titles ∩ component membership) — drop the git variant. Note that journals are out of scope by default (the user-page query excludes `:block/journal-day`); name that exclusion in the coverage section.

- [ ] **Step 4: Rewrite §8 — Apply + annotate**

Keep the policy table and "Things you NEVER do" verbatim (they are substrate-independent), but:
- Earned wikilink row: "add `[[Target]]` into the relevant block via `logseq upsert block`" (not a `.md` edit).
- Add a **one-time schema bootstrap** paragraph (the four `upsert tag`/`upsert property` commands from the reference), noting it is idempotent and must precede the first flag because Logseq requires tag/property schema to pre-exist, and that all three properties are `type default` (text) — `date` rejects a plain `YYYY-MM-DD` and partial-writes.
- Annotation paragraph: flags are written as a **child block tagged `#curator`**, attached to the specific offending **block** (via `--target-id <id> --pos last-child`, using the block id a digest/verify worker reported) when known, else at page level. Give the exact `upsert block … --update-tags '["curator"]' --update-properties '{…}'` command.

- [ ] **Step 5: Rewrite §9 — Annotation format & idempotency**

Replace the three `> [!curator]` callout templates with the `#curator`-block content + property mapping for each flag type:
- Contradiction: content `Contradiction: claim "X" here conflicts with [[Other]]; evidence cited both sides; resolving needs research/RE/domain analysis beyond curation. — wiki-curator`; props `{:curator-type "contradiction" :curator-flagged "<YYYY-MM-DD>" :curator-conflicts-with "Other"}`.
- Stale: content `Stale claim: "X" may be superseded by [[Newer]]. A domain specialist should verify currency. — wiki-curator`; props `{:curator-type "stale" :curator-flagged "<date>" :curator-conflicts-with "Newer"}`.
- Ambiguous: content `Ambiguous claim: "X" lacks sufficient cited evidence to verify. A domain specialist should confirm or source it. — wiki-curator`; props `{:curator-type "ambiguous" :curator-flagged "<date>"}` (no conflicts-with; page-level attach is fine).

Rewrite the idempotency protocol to use the **portable enumeration query** (tag `:block/name "curator"` + host page + content; plus the `:block/refs`/`(not= ?r ?t)` variant for conflicts-with). Steps: (1) enumerate existing `#curator` flags; (2) if one exists for the same (host page/block, conflicting page) and the conflict still holds → leave it (optionally refresh `curator-flagged`), do NOT duplicate; (3) if it no longer holds → `logseq remove block --id <id>` (convergence shrinks the annotation surface); (4) if none → write it. **Add the boxed warning: never hardcode `:user.property/*` / `:user.class/*` idents — they carry a random per-graph suffix; match on tag name + refs.** Keep the "annotation is knowledge, not a log" closing.

- [ ] **Step 6: Rewrite §11 — Worker prompt templates**

In all three templates: `vault=<NAME>` → `graph=<NAME>` (+ `root_dir=` note). Digest/Verify workers read via `logseq show --graph <NAME> [--root-dir <R>] --page "<Page>" --linked-references true`; instruct workers to **report the block id** for any claim they flag (so the curator can block-attach the annotation). Re-word the outbound-wikilink / dangling-reference digest sections to Logseq page/ref vocabulary. For the Island Reconnect template: keep the `vault-reconciler` role but re-word to Logseq; add a one-line note that if atom does not register a `vault-reconciler` sub-agent type, the curator dispatches a `general-purpose` worker with the same read-only proposal contract (this is resolved in Task 4).

- [ ] **Step 7: Vocabulary sweep of §2, §4, §5, §6, §7, §10, §12**

Read each and replace substrate nouns only: "vault"→"graph", "note"→"page" (where it means a KB entity, not the verb), "`[[wikilink]]` in `.md`"→"`[[page ref]]`", "`> [!curator]` callout"→"`#curator` block", "re-run `find-disconnected-notes.py`"→"re-run the Sense island query + `find-disconnected-pages.py`". Do NOT touch the methodology logic. In §12, the "No vault logs" invariant → "No graph logs"; the "`curate-<domain>` convention" invariant is unchanged.

- [ ] **Step 8: Verify no Obsidian residue remains in SKILL.md**

Run:
```bash
cd /Users/kev/gitclones/atom
grep -niE 'obsidian|/mnt/user-data|/mnt/skills/public/obsidian|kiwi\.vault|\[!curator\]|find-recently-modified|find-disconnected-notes|_vault_ids|since-git|\.md\b' skill_library/curate-knowledge-base/SKILL.md
```
Expected: no output (exit 1). If any line prints, fix it and re-run.

- [ ] **Step 9: Commit**

```bash
git add skill_library/curate-knowledge-base/SKILL.md
git commit -m "feat(skill): port curate-knowledge-base SKILL.md to Logseq DB

Rewrite the substrate (graph acquisition, Sense via logseq query, #curator
tagged-block flags with portable tag+refs idempotency, show --page worker
read). Preserve the map-reduce methodology and all invariants. Drop the
git incremental mode. Vocabulary swept vault->graph, note->page."
```

---

## Task 4: Delete obsolete scripts + resolve `vault-reconciler`

Remove the three file-walking scripts and confirm the SKILL no longer names them. Confirm whether atom registers a `vault-reconciler` sub-agent type and lock the §11 wording accordingly.

**Files:**
- Delete: `scripts/find-disconnected-notes.py`, `scripts/find-recently-modified-notes.py`, `scripts/_vault_ids.py`

- [ ] **Step 1: Check for a `vault-reconciler` sub-agent type**

Run: `grep -rniE 'vault-reconciler|vault_reconciler' src/ docs/ skill_library/ 2>/dev/null | grep -v SKILL.md`
Expected: determines availability. If there is NO registration outside SKILL.md, the §11 note added in Task 3 Step 6 (degrade to `general-purpose`) stands — verify that note is present in `SKILL.md`; if a real `vault-reconciler` type exists, tighten §11 to require it. Make the one-line edit if needed.

- [ ] **Step 2: Delete the three obsolete scripts**

```bash
git rm skill_library/curate-knowledge-base/scripts/find-disconnected-notes.py \
       skill_library/curate-knowledge-base/scripts/find-recently-modified-notes.py \
       skill_library/curate-knowledge-base/scripts/_vault_ids.py
```

- [ ] **Step 3: Confirm nothing references the deleted scripts**

Run:
```bash
cd /Users/kev/gitclones/atom
grep -rniE 'find-disconnected-notes|find-recently-modified|_vault_ids' skill_library/ && echo "RESIDUE" || echo "CLEAN"
ls skill_library/curate-knowledge-base/scripts/
```
Expected: `CLEAN`; `scripts/` lists only `find-disconnected-pages.py`.

- [ ] **Step 4: Commit**

```bash
git add -A skill_library/curate-knowledge-base/scripts
git commit -m "chore(skill): remove Obsidian file-walk scripts

find-disconnected-notes.py, find-recently-modified-notes.py and _vault_ids.py
are obsolete under the Logseq DB: island detection is now a logseq query fed
to find-disconnected-pages.py, and change detection is native updated-at."
```

---

## Task 5: End-to-end verification against a live Logseq graph (manual/local)

Prove the documented substrate actually works, using the `logseq` CLI. Not committed (CI has no `logseq`). Run every command; each expected output is stated.

**Files:** none (verification only).

- [ ] **Step 1: Build a fixture graph**

```bash
ROOT=/private/tmp/claude-501/-Users-kev-gitclones-atom/da296383-aed7-4b70-b67c-ec42cf065001/scratchpad/lsq-verify
logseq graph create --graph curate-verify --root-dir "$ROOT"
for c in "Alpha relates to [[Beta]]." "Beta builds on [[Gamma]]." "Gamma loops to [[Alpha]]." \
         "IslandX pairs with [[IslandY]]." "IslandY pairs with [[IslandX]]."; do :; done
logseq upsert block --graph curate-verify --root-dir "$ROOT" --target-page Alpha   --content "Alpha relates to [[Beta]]."
logseq upsert block --graph curate-verify --root-dir "$ROOT" --target-page Beta    --content "Beta builds on [[Gamma]]."
logseq upsert block --graph curate-verify --root-dir "$ROOT" --target-page Gamma   --content "Gamma loops to [[Alpha]]."
logseq upsert block --graph curate-verify --root-dir "$ROOT" --target-page IslandX --content "IslandX pairs with [[IslandY]]."
logseq upsert block --graph curate-verify --root-dir "$ROOT" --target-page IslandY --content "IslandY pairs with [[IslandX]]."
logseq upsert block --graph curate-verify --root-dir "$ROOT" --target-page Lonely  --content "Lonely has no links."
```
Expected: each `upsert` returns `{"status":"ok",...}`.

- [ ] **Step 2: Run the Sense island pipeline exactly as SKILL.md documents**

```bash
logseq query --graph curate-verify --root-dir "$ROOT" --output json --query \
'[:find ?t (count ?b) :where [?p :block/name] [?p :block/title ?t] (not [?p :db/ident]) (not [?p :block/journal-day]) [?b :block/page ?p]]' > "$ROOT/pages.json"
logseq query --graph curate-verify --root-dir "$ROOT" --output json --query \
'[:find ?fp ?tp :where [?b :block/page ?f] [?f :block/title ?fp] [?b :block/refs ?t] [?t :block/title ?tp] [?t :block/name]]' > "$ROOT/edges.json"
python3 skill_library/curate-knowledge-base/scripts/find-disconnected-pages.py \
  --pages-json "$ROOT/pages.json" --edges-json "$ROOT/edges.json" --json
```
Expected JSON: `component_count` 3, `main_component.size` 3 (`Alpha,Beta,Gamma`), `islands` one entry `{size:2, members:[IslandX,IslandY]}`, `isolated` `["Lonely"]`, `orphans`/`deadends` contain `Lonely`.

- [ ] **Step 3: Exercise the full flag lifecycle**

```bash
# bootstrap schema
logseq upsert tag      --graph curate-verify --root-dir "$ROOT" --name curator
logseq upsert property --graph curate-verify --root-dir "$ROOT" --name curator-type           --type default
logseq upsert property --graph curate-verify --root-dir "$ROOT" --name curator-flagged        --type default
logseq upsert property --graph curate-verify --root-dir "$ROOT" --name curator-conflicts-with --type default
# write a contradiction flag
logseq upsert block --graph curate-verify --root-dir "$ROOT" --target-page Alpha \
  --content 'Contradiction: claim here conflicts with [[Beta]]; needs domain analysis. — wiki-curator' \
  --update-tags '["curator"]' \
  --update-properties '{:curator-type "contradiction" :curator-flagged "2026-07-19" :curator-conflicts-with "Beta"}'
# enumerate (idempotency sees it)
logseq query --graph curate-verify --root-dir "$ROOT" --output json --query \
'[:find ?host ?content :where [?t :block/name "curator"] [?b :block/tags ?t] [?b :block/page ?hp] [?hp :block/title ?host] [?b :block/title ?content]]'
```
Expected: the flag block renders `#curator` with three properties; the enumeration returns one row `["Alpha","Contradiction: … [[Beta]] …"]`.

- [ ] **Step 4: Convergence — remove the flag and confirm it is gone**

```bash
FLAG_ID=$(logseq query --graph curate-verify --root-dir "$ROOT" --output json --query \
'[:find ?b :where [?t :block/name "curator"] [?b2 :block/tags ?t] [(identity ?b2) ?b]]' | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['result'][0][0])")
logseq remove block --graph curate-verify --root-dir "$ROOT" --id "$FLAG_ID"
logseq query --graph curate-verify --root-dir "$ROOT" --output json --query \
'[:find ?host ?content :where [?t :block/name "curator"] [?b :block/tags ?t] [?b :block/page ?hp] [?hp :block/title ?host] [?b :block/title ?content]]'
```
Expected: after removal the enumeration returns `{"data":{"result":[]}}`.

- [ ] **Step 5: Full banned-token sweep of the skill directory**

```bash
cd /Users/kev/gitclones/atom
grep -rniE 'obsidian|/mnt/user-data|/mnt/skills/public/obsidian|kiwi\.vault|\[!curator\]|since-git' skill_library/curate-knowledge-base/ && echo "RESIDUE — FIX" || echo "CLEAN"
```
Expected: `CLEAN`.

- [ ] **Step 6: Run the full atom test suite (no regressions)**

Run: `cd /Users/kev/gitclones/atom && .venv/bin/python -m pytest -q`
Expected: all tests pass, including the new `tests/test_curate_disconnected_pages.py`.

- [ ] **Step 7: Clean up the scratch graph**

```bash
rm -rf /private/tmp/claude-501/-Users-kev-gitclones-atom/da296383-aed7-4b70-b67c-ec42cf065001/scratchpad/lsq-verify
```

---

## Self-Review

**Spec coverage** (each spec section → task):
- Port in place, Logseq-only, Obsidian dropped → Tasks 1–4 (baseline → script → SKILL → delete).
- Scripts fate (delete 2, slim 1) → Task 2 (new `find-disconnected-pages.py`) + Task 4 (delete 3 old).
- §1 graph acquisition (`graph=<NAME>`, no default) → Task 3 Step 2.
- §3 Sense command mapping (verified queries) → Task 3 Step 3 + reference block.
- §8/§9 `#curator` block flags, schema bootstrap, portable idempotency, convergence remove → Task 3 Steps 4–5 + Task 5 Steps 3–4.
- §11 worker read primitive + `vault-reconciler` degrade → Task 3 Step 6 + Task 4 Step 1.
- §2/§10/§12 vocabulary → Task 3 Step 7.
- `since_git` removed → Task 3 Step 2, Global Constraints.
- Testing (pure unit test in CI + manual fixture smoke test) → Task 2 + Task 5.
- Risks: property names/types finalized (`curator-*`, all `type default`) → Task 3 Step 4/5; built-in filtering predicate → verified query in reference; `list node` over-match avoided → portable tag query.

**Placeholder scan:** all commands are the verified, literal forms; the one Python script is shown in full; test code is complete. The `<skill_dir>`, `<NAME>`, `<R>`, `<Page>`, `<blockId>`, `<YYYY-MM-DD>` tokens are runtime parameters (documented as such in the reference), not authoring placeholders.

**Type consistency:** `analyze(pages, edges) -> dict` keys are identical across the script, its test, and the §3/§5 Step 2 verification. The report keys `component_count`/`main_component`/`islands`/`isolated`/`orphans`/`deadends` match between Task 2, Task 3 Step 3, and Task 5 Step 2. Property names `curator-type`/`curator-flagged`/`curator-conflicts-with` are identical in the bootstrap, the write command, and every flag template.
