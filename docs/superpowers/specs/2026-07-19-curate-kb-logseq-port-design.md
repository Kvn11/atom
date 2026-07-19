# Port `curate-knowledge-base` skill from Obsidian to Logseq — design

Date: 2026-07-19
Status: approved (ready for implementation plan)

## Summary

`skill_library/curate-knowledge-base/` is a newly-added, provider-agnostic **curation methodology**
(a `wiki-curator` lead agent runs a map-reduce pass over a knowledge base: Sense → Partition → Map →
Reduce → Verify → Apply → Report, under "earned-edits-only" and "detect-and-flag, never adjudicate"
invariants). It was imported from a different harness ("kiwi") and is coupled to **Obsidian** — a
bespoke `obsidian vault=<NAME> …` CLI, `/mnt/user-data/<NAME>` paths, `[[wikilink]]`-regex-over-`.md`
files, YAML frontmatter, and `> [!curator]` callouts.

atom's own notes backend is **Logseq**, and specifically a Logseq **DB graph** driven entirely
through the `logseq` CLI (`src/atom/notes.py` provisions a per-workflow graph via `logseq graph
create`; any `provider != "logseq"` raises `NotImplementedError`). None of the Obsidian skill's
substrate exists in atom.

**Decision (confirmed with the user): port the single skill *in place* to the Logseq DB and drop
Obsidian entirely.** No clone, no rename, no dual-vault support — atom cannot run an Obsidian vault,
so a shipped Obsidian variant would be non-functional cruft. The ~80% that is genuine methodology is
preserved verbatim; only the *substrate* (Sense, read, annotate, change-detection) is re-grounded on
the `logseq` CLI + DB semantics.

The port is **not** a syntax find-and-replace. The Obsidian skill reads `.md` files off disk and
builds the link graph in Python; a Logseq DB graph has no such files — the graph, backlinks, tags,
properties, and timestamps are all first-class DB entities queried through the CLI. So the entire
*Sense* substrate inverts, and two of the three bundled Python scripts dissolve.

## Background: what exists today

(Established by reading `skill_library/curate-knowledge-base/SKILL.md` and its `scripts/`,
`src/atom/notes.py`, and by empirically probing the `logseq` CLI against a throwaway graph.)

### The skill as shipped (Obsidian)

- `SKILL.md` — 12 sections. §1 preconditions (vault from prompt, no default); §2 `curate-<domain>`
  flavor lens; §3 Sense; §4 Partition; §5 Map (fan-out digest workers); §6 Reduce; §7 Verify; §8
  Apply+annotate (single writer); §9 annotation format + idempotency; §10 Report + converge; §11
  worker prompt templates; §12 invariants recap.
- `scripts/_vault_ids.py` — shared node-id / file-collection helpers; exists solely so the two
  scripts below agree on relpath-derived node ids (the curator intersects their outputs).
- `scripts/find-disconnected-notes.py` — walks `.md` files, regex-parses `[[wikilinks]]`, builds the
  undirected link graph, reports connected components (islands). Wikilink parsing is ported verbatim
  from a non-existent `kiwi.vault.graph`.
- `scripts/find-recently-modified-notes.py` — lists notes changed since a timestamp (filesystem
  mtime) or a git ref (`--since-git`), for incremental passes.

The skill references `/mnt/skills/public/obsidian-lint/scripts/…` and `/mnt/user-data/<NAME>` —
neither path exists in atom. The `obsidian vault=<NAME> folders|tags|orphans|deadends|unresolved|
files|read` CLI is bespoke to the kiwi harness and is **not** present in atom (the `/usr/local/bin/
obsidian` on the dev Mac is Obsidian.app's launcher, which has none of those subcommands).

### atom's Logseq backend

- `src/atom/notes.py` — `ensure_vault()` provisions a per-workflow Logseq **graph** at
  `$ATOM_HOME/notes/<workflow-slug>/` (graph name defaults to the slug) via `logseq graph create
  --graph <g> --root-dir <root>`; idempotent list-then-create. Only `provider="logseq"` is
  supported.
- The `logseq` CLI (verified `--help`): `graph`, `list {page,tag,property,task,node,asset}`,
  `upsert {block,page,task,asset,tag,property}`, `remove {block,page,tag,property}`, `search`,
  `query` (Datascript), `show` (tree), `server` (db-worker-node). This is a **database** graph
  (Datascript, `:block/name`, `:block/updated-at`, tags/properties as first-class entities,
  block-level addressing by id/uuid) — not a folder of markdown.

### Empirically verified `logseq` CLI semantics (throwaway graph)

All of the following were run and confirmed against a scratch `curate-probe` graph:

- **`[[wikilinks]]` in block content auto-create the target page and a ref.** `upsert block
  --target-page Alpha --content "… [[Beta]] …"` returned ids for both Alpha and the auto-created
  Beta.
- **Page→page reference edges via Datascript** (the input a component-finder needs), tags and
  built-ins excluded:
  ```
  logseq query --graph <g> --root-dir <r> --output json --query \
    '[:find ?fp ?tp :where [?b :block/page ?f] [?f :block/title ?fp]
                           [?b :block/refs ?t] [?t :block/title ?tp] [?t :block/name]]'
  → [["Gamma","Alpha"],["Alpha","Beta"],["Beta","Gamma"],["IslandY","IslandX"],["IslandX","IslandY"]]
  ```
- **`list page` includes built-in `logseq.class/*` pages** (`db/ident` prefixed `logseq.`) that must
  be filtered out when enumerating real notes.
- **Tags/properties are schema and must be declared before use.** `upsert block --update-tags
  '["curator"]'` failed `tag-not-found` until `upsert tag --name curator` was run once; likewise
  `upsert property --name <p> --type default`.
- **Annotation write**: `upsert block --target-page X --content "…" --update-tags '["curator"]'`
  succeeds once the tag exists; `--update-properties '{…}'` attaches block/page properties.
- **Enumerate open flags natively** (idempotency + convergence):
  ```
  logseq query … --query '[:find ?ptitle ?content :where [?t :block/name "curator"]
      [?b :block/tags ?t] [?b :block/title ?content] [?b :block/page ?p] [?p :block/title ?ptitle]]'
  → [["Alpha","Contradiction: … conflicts with [[Beta]] … — wiki-curator"]]
  ```
  (`list node --tags curator` also works but can surface referenced pages too; the Datascript form
  is exact. Filter to `node/type == "block"` if using `list node`.)
- **Read a page + block tree + backlinks** (worker read primitive): `show --page X
  --linked-references true` renders each block with its **id** (so block-granular annotation is
  free) and a "Linked References" (backlinks) section.
- **Remove a resolved flag** (convergence): `remove block --id <id>` / `--uuid <uuid>`.

## Goals / non-goals

**Goals**

1. `curate-knowledge-base` runs correctly against an atom Logseq DB graph, preserving the full
   methodology and every invariant that is not substrate-specific.
2. The Sense stage uses native `logseq` CLI queries; island detection is computed from a real
   page→page edge query.
3. Flags (contradictions, stale, ambiguous) are written as queryable `#curator` blocks; idempotency
   and convergence are DB queries, not note-body scans.
4. The skill is self-contained and self-consistent: no dangling references to Obsidian paths, the
   `obsidian` CLI, `kiwi.vault.graph`, or deleted scripts.

**Non-goals**

- No clone / rename / Obsidian variant. No second skill.
- No new atom backend feature. `notes.py` is untouched; the skill takes its graph from its prompt
  and does not hard-wire atom's `$ATOM_HOME/notes/<slug>` layout.
- No change to the map-reduce control flow, the sub-agent roster, or the "single writer" model.
- `since_git` incremental mode is dropped (a DB graph is not markdown-under-git). No git support.

## Design

### Overall shape

One skill, same name and directory. `SKILL.md` keeps its 12-section structure and all
provider-agnostic prose. Substrate sections (§1, §3, §8, §9, §11) are rewritten; §2, §4, §5, §6, §7,
§10, §12 change only where they name a substrate command or the `[!curator]` format.

### Scripts

| Script | Fate |
|---|---|
| `_vault_ids.py` | **Delete.** Its sole purpose (two scripts agreeing on relpath-derived node ids) is moot — the DB is the id authority (`:block/name`), and only one script remains. |
| `find-recently-modified-notes.py` | **Delete.** Change detection is native `:block/updated-at`. |
| `find-disconnected-notes.py` | **Slim & rename → `find-disconnected-pages.py`.** Keep only the connected-components BFS (`connected_components` / `_components_from_adjacency` / `analyze_vault` report shape). Replace the input: instead of walking `.md` files and regex-parsing wikilinks, read the page→page edge list produced by `logseq query` (from a `--edges-json <file>` arg or stdin) plus the full page list (for singletons/isolated). Drop `parse_wikilinks`, the code-span regexes, `collect_md_files`, and all `kiwi.vault.graph` porting comments. The output JSON shape (`note_count`, `edge_count`, `component_count`, `main_component`, `islands`, `isolated`) is preserved so the methodology text in §3/§6/§10 is unchanged. Node ids become **page titles** (from the DB), not relpaths.

The new script is pure-stdlib and takes structured input from the CLI; it performs no I/O against the
graph itself (the curator runs `logseq query` and pipes the JSON in), keeping the graph-access
contract in the SKILL.md commands and the algorithm in the script.

### §1 — Preconditions & graph (rewritten)

- The unit of work is a Logseq **graph**, named in the prompt as `graph=<NAME>` (optionally
  `root_dir=<PATH>` when the graph is not under the CLI default `~/logseq`). **No default graph** —
  if none is named, STOP and return a report; never guess. (Preserves the original "vault from
  prompt, no default" invariant, re-expressed for Logseq. Works for an atom workflow's own notes
  graph — pass the workflow slug — without hard-wiring atom's layout.)
- Confirm the graph exists and is reachable: `logseq graph list --output json` (and/or `logseq
  graph info --graph <NAME>`). If absent/errors, STOP and report that graph `<NAME>` was not found;
  graph acquisition is the caller's responsibility.
- Accept the same optional prompt params: `domain=<domain>`, `since=<ISO|epoch>` (now interpreted
  against `:block/updated-at`), `full`, `max_passes=<N>`. `since_git` is **removed**.

### §3 — Sense (rewritten; verified command mapping)

| Purpose | Obsidian (removed) | Logseq (ported) |
|---|---|---|
| Islands / components | `find-disconnected-notes.py <path> --json` | `logseq query --graph <g> [--root-dir <r>] --output json --query '<page→page edges>'` piped to `find-disconnected-pages.py` |
| Tag stats | `obsidian … tags counts` | `logseq list tag --graph <g> --output json` |
| Property stats | `obsidian … properties counts` | `logseq list property --graph <g> --output json` |
| Orphans / dead-ends | `obsidian … orphans` / `deadends` | derive from the edge set + `list page`: page with no inbound edge = orphan; no outbound = dead-end |
| Full note listing | `obsidian … files` | `logseq list page --graph <g> --output json`, filtering built-ins (`db/ident` prefixed `logseq.`) |
| Recently changed | `find-recently-modified-notes.py … --since` | `logseq list node --graph <g> --sort updated-at --order desc --output json` (or a Datascript `updated-at > threshold` filter) |

The exact page→page edge query is the verified one above. Sense still produces no graph writes — only
the curator's internal terrain map. The recorded terrain (component count before/after, islands,
orphans, dead-ends) is unchanged in meaning, so §4/§6/§10 that consume it are untouched.

### §8/§9 — Apply + annotate (rewritten substrate, same policy)

Policy is unchanged: the curator is the single writer; workers propose, curator applies; earned
edits only; **flags are written but contradictions/stale/ambiguous claims are never resolved**;
dual-channel surfacing (in-graph flag + caller report); no activity/skip/coverage logs in the graph.

Substrate changes:

- **Earned wikilink** — add `[[Target]]` into the relevant block via `upsert block` (`--content`
  edit or `--target-block`/`--pos` child), rather than editing a `.md` file.
- **One-time schema bootstrap** (idempotent, run before the first flag): `upsert tag --name curator`;
  `upsert property --name curator-type --type default`; `upsert property --name curator-flagged
  --type date`; `upsert property --name curator-conflicts-with --type default` (names TBD-final in
  the plan, but declared up front because Logseq requires tag/property schema to pre-exist).
- **Flag annotation** — replace the `> [!curator]` callout with a **child block tagged `#curator`**,
  attached to the specific offending **block** when the digest names one (Logseq gives block ids via
  `show`), else to the page as a top-level block. Written via:
  ```
  logseq upsert block --graph <g> --target-page <P> [--target-id <block> --pos last-child] \
    --content "Contradiction: claim \"X\" here conflicts with [[Other]]; resolving needs domain \
    analysis. — wiki-curator" \
    --update-tags '["curator"]' \
    --update-properties '{:curator-type "contradiction" :curator-flagged "<YYYY-MM-DD>" \
                          :curator-conflicts-with "Other"}'
  ```
  Stale and ambiguous flags use the same shape with `:curator-type "stale" | "ambiguous"` and the
  corresponding message text (ported from the three §9 templates).
- **Idempotency protocol** (rewritten) — before writing a flag, enumerate existing flags with the
  verified Datascript query (`[?t :block/name "curator"] [?b :block/tags ?t] …`) and check whether
  a flag for the same conflict (same host page/block + same `curator-conflicts-with`) already
  exists. If it exists and the conflict still holds → leave it (optionally refresh
  `curator-flagged`). If it no longer holds → `remove block` the stale flag (this is how convergence
  shrinks the annotation surface). If none exists → write it.

### §11 — Worker templates (substrate-only edits)

- Every template's `vault=<NAME>` → `graph=<NAME>` (+ `root_dir=` when relevant).
- Digest / Verify workers read via `logseq show --graph <g> --page <P> --linked-references true`
  (block tree + backlinks) instead of `obsidian … read path=`. Workers report block ids for any
  claim they flag, so the curator can attach a block-granular annotation.
- The Island Reconnect `vault-reconciler` template keeps its role; its prompt is re-worded to
  Logseq page/graph vocabulary. (Whether atom ships a `vault-reconciler` sub-agent type is a plan-
  time check; if not present, the reconciler step degrades to a general-purpose worker with the
  same read-only proposal contract — flagged in the plan.)

### §2, §10, §12 — provider-agnostic (minimal edits)

- §2 `curate-<domain>` flavor convention is **preserved unchanged** — it is substrate-independent.
- §10 Report + converge: unchanged except the convergence check re-runs Sense (now the Logseq
  commands) and reads component-count-before/after from `find-disconnected-pages.py`.
- §12 invariants: reworded where they say "vault"/"note"/`[!curator]` → "graph"/"page"/"`#curator`
  block", but every invariant survives. The "no vault logs" invariant becomes "no graph logs": the
  `#curator` flag is knowledge, not a log; activity/skip/coverage logs stay in the caller report.

## Testing

The Logseq CLI is available on the dev machine and was used to verify every substrate primitive
above. The implementation plan will:

1. Build a fixture graph (main cluster + a 2-page island + an orphan + a property), as done during
   design, under a scratch `--root-dir`.
2. Assert `find-disconnected-pages.py` on the fixture's edge query reports exactly one main
   component, one island (the 2 pages), and one isolated page — matching the pre-port
   `find-disconnected-notes.py` semantics on an equivalent Obsidian vault.
3. Exercise the full flag lifecycle: bootstrap schema → write a contradiction flag on a block →
   enumerate via the Datascript query (idempotency sees it) → re-run (no duplicate) → remove
   (convergence) → enumerate (gone).
4. Grep the finished `SKILL.md` and script for any surviving `obsidian`, `/mnt/user-data`,
   `kiwi.vault`, `.md`-walk, or `[!curator]` reference (must be zero).

`find-disconnected-pages.py` gets unit coverage in the atom test suite mirroring the existing
`tests/` conventions (edge-list-in → components-out), since it is now pure and CLI-fed.

## Risks / open items for the plan

- **Exact property names / types** (`curator-type`, `curator-flagged`, `curator-conflicts-with`) and
  whether flags carry a page-property vs block-property — finalize in the plan against a live graph.
- **`vault-reconciler` sub-agent availability** in atom — confirm or degrade gracefully.
- **`list node --tags` over-matching** referenced pages — the plan standardizes on the exact
  Datascript enumeration to avoid counting non-flag nodes.
- **Built-in page filtering** — confirm the precise predicate (`db/ident` prefix `logseq.`) covers
  all built-ins (`logseq.class/*` and any others) so Sense counts only real notes.
