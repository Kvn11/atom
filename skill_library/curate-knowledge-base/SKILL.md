---
name: curate-knowledge-base
description: Domain-agnostic methodology for the wiki-curator lead agent to clean, organize, and connect a Logseq knowledge base (DB graph, via the `logseq` CLI) at scale via map-reduce over sub-agents. Senses the graph, partitions pages into groups, fans out digest sub-agents, reduces digests to find cross-group links and contradictions, applies earned organizational edits, and FLAGS (never resolves) contradictions/stale claims by annotating in-graph and reporting to the caller. The curator reads this after skill_search; if the phase names a domain it also skill_searches `curate-<domain>` for a lens. Never writes operational logs to the graph.
---

# curate-knowledge-base

You are a **wiki-curator lead agent**. You orchestrate a map-reduce curation pass over a Logseq DB graph. Read this skill in full before taking any action. You will dispatch sub-agents; you are the single writer.

---

## § 1 — Preconditions & graph

Your unit of work is a Logseq **DB graph**, named in your prompt as `graph=<NAME>`. Optionally the prompt also gives `root_dir=<PATH>` when the graph does not live under the `logseq` CLI default (`~/logseq`). **There is no default graph.** If no graph is named, STOP immediately and return a report explaining that no graph was specified; do not proceed and never guess.

Once you have the graph name:

1. Confirm the graph exists and is accessible:
   ```bash
   logseq graph list --output json
   # and/or, to inspect one graph:
   logseq graph info --graph <NAME> [--root-dir <PATH>] --output json
   ```
   If the graph is absent from the list or these commands error, STOP and report that graph `<NAME>` could not be found. Do not guess an alternate location; graph acquisition is the caller's responsibility, not yours.

2. **Invariant — every subsequent `logseq` call carries `--graph <NAME>`** (and `--root-dir <PATH>` whenever `root_dir` was given in your prompt). Add `--output json` to any command whose output you parse. In the commands below, include the bracketed `[--root-dir <PATH>]` only when `root_dir` was provided.

3. Accept these optional prompt parameters:
   - `domain=<domain>` — activates a `curate-<domain>` flavor lens (see §2).
   - `since=<ISO-timestamp|epoch>` — activates incremental mode (see §3); interpreted against each page's `:block/updated-at`.
   - `full` — explicitly requests a full pass (ignores any `since`).
   - `max_passes=<N>` — override the convergence pass bound (default: 3).

---

## § 2 — Flavor lookup

You know the naming convention: domain-specific curation lenses are skills named `curate-<domain>`. If your prompt includes `domain=<domain>` (and `<domain>` is not `generic`), do the following **before Stage 1**:

1. `skill_search` for `curate-<domain>`.
2. If the skill exists, read it. It is a thin overlay that tells you: what counts as a key entity/finding for this domain; domain-specific contradiction patterns to watch for; annotation vocabulary customizations.
3. Apply that lens throughout all stages — it adjusts what you notice and how you annotate, but it does not replace this methodology.
4. If the domain skill is not found, proceed with generic methodology and note in your report that `curate-<domain>` was not found.

If no `domain` parameter is given, or `domain=generic`, skip the flavor lookup entirely.

---

## § 3 — Stage 1: Sense (whole-graph, cheap, no page bodies read)

The goal of Sense is to build a terrain map of the graph in a few KB — structure, statistics, problem areas — without reading any page body in full. The Logseq DB already holds the link graph, so Sense is a handful of Datascript queries; nothing walks page bodies.

**Always run these commands.** First capture the two graph-shape queries — the real user pages, and the page→page reference edges — then feed both to the island detector:

```bash
# Real user pages: non-built-in, non-journal, >=1 block → [["Alpha",2],["Beta",1],…]
logseq query --graph <NAME> [--root-dir <PATH>] --output json --query \
'[:find ?t (count ?b) :where [?p :block/name] [?p :block/title ?t] (not [?p :db/ident]) (not [?p :block/journal-day]) [?b :block/page ?p]]' > /tmp/pages.json

# Page→page reference edges (tags & built-ins excluded) → [["Gamma","Alpha"],["Alpha","Beta"],…]
logseq query --graph <NAME> [--root-dir <PATH>] --output json --query \
'[:find ?fp ?tp :where [?b :block/page ?f] [?f :block/title ?fp] [?b :block/refs ?t] [?t :block/title ?tp] [?t :block/name]]' > /tmp/edges.json

# Island detection — every disconnected component, from the DB's own link graph
python3 <skill_dir>/scripts/find-disconnected-pages.py \
    --pages-json /tmp/pages.json --edges-json /tmp/edges.json --json

# Tag and property statistics
logseq list tag      --graph <NAME> [--root-dir <PATH>] --output json
logseq list property --graph <NAME> [--root-dir <PATH>] --output json
```

Record from the `find-disconnected-pages.py` report:
- Total page count (`note_count`) and component count (`component_count`, i.e. N_components_before). You will re-run this same Sense pipeline after applying edits to get N_components_after.
- The main connected graph (`main_component`) and every disconnected **island** (`islands` — components of size ≥ 2 that share no path to the main component). A single-page cluster with no resolved links (an entry in `isolated`) is also an island.
- Orphan pages (`orphans` — no inbound reference) and dead-end pages (`deadends` — no outbound reference). Both are derived by `find-disconnected-pages.py` from the edge list; no separate link-health command is needed.

**If incremental (`since` provided, no `full` flag):**

```bash
# Nodes, most-recently changed first; filter to those updated at/after `since`
logseq list node --graph <NAME> [--root-dir <PATH>] --sort updated-at --order desc --output json
```

`since` is interpreted against `:block/updated-at`. Take the page titles whose most recent block update is at/after `since`, then intersect that changed-page set with the component-membership list from `find-disconnected-pages.py`. For each changed page, include its entire component (and hub-neighborhood, if applicable) in the working set so context is never partial. Pages outside the working set are **out of scope for this pass** — name them in the skip/coverage section of your report.

Journals are out of scope by default: the user-page query above excludes `:block/journal-day`, so journal pages are never curation targets. Name that exclusion in the coverage section of your report.

Sense produces no graph writes. It produces only your internal terrain map.

---

## § 4 — Stage 2: Partition

Split the working set into sub-agent-sized groups. A group is sized so one general-purpose worker can read every page in it within context (target: ≤ 40 pages per group, or ≤ ~100 KB of page content; tune downward if pages are long).

Partitioning order:

1. **Connected components first.** Each connected component from `find-disconnected-pages.py` is a natural group boundary. If a component exceeds the size bound, split it by hub-neighborhoods: pick the highest-degree page in the component as a hub, assign it and its 1-hop neighbors as one group, and recursively partition the remainder.
2. **Folder/tag clusters next.** Pages in the same folder or sharing a dominant tag form a natural group, especially for orphans or pages with only folder-based cohesion.
3. **Residual pages** (orphans not captured above) go into small groups of ≤ 20.

Label every group (e.g., `group-1-component-A`, `group-7-folder-security`). You will use these labels in the Reduce and Report stages.

Do not read page bodies during Partition. Use only the page titles and link graph from Sense.

---

## § 5 — Stage 3: Map (fan out)

Dispatch **one `general-purpose` sub-agent per group** in a single response. All workers are read-only. Include `graph=<NAME>` (and `root_dir=<PATH>` when given) in every worker prompt. Use the Digest Worker template from §11.

Issue all digest dispatches in one batch (a single response with N `task()` calls, one per group) so they run in parallel. Wait for all digests to return before proceeding to Reduce.

Do not read page bodies yourself during this stage; the workers do the reading. Your job is to collect and synthesize their structured digests.

---

## § 6 — Stage 4: Reduce (you, the lead)

You now hold N small digests — structured summaries, not full page text. This fits in context even for hundreds of pages.

Derive candidates from the digests:

**Cross-group link candidates.**
A dangling reference in group A (an entity/term mentioned but not linked) that matches a page or entity in group B → candidate earned wikilink. Record: source page, target page, group A, group B, the referencing text snippet from the digest.

**Missing concept page candidates.**
An entity recurring in 3+ group digests with no canonical page anywhere → candidate for a new concept/MOC page. Do NOT auto-create it; record for the report.

**Contradiction candidates.**
A claim in group A that directly conflicts with a claim in group B (same entity, opposite or incompatible assertions, both with cited evidence) → candidate contradiction. Record both pages, both claims, both evidence citations.

**Stale / ambiguous claim candidates.**
A claim in a digest that cites NO evidence (ambiguous/unverified), or that cites evidence a more-recent page supersedes (stale per `:block/updated-at` or log timestamps in the digest) → candidate stale or ambiguous claim. Record: the page, the claim text, and the reason (no cited evidence, or specifically-named newer page that supersedes it).

**Island reconnection.**
For each disconnected island identified in Sense, dispatch one `vault-reconciler` sub-agent per island (see §11). The reconciler proposes reconnection links; it never writes. Collect its structured proposals.

Contradictions and stale/ambiguous claims identified here are **candidates only** — they must be verified in Stage 5 before any annotation is written.

---

## § 7 — Stage 5: Verify

For each candidate link and each candidate contradiction, dispatch a focused Verify Worker (general-purpose sub-agent; see §11). These workers read only the two relevant pages plus any cited evidence. They return a yes/no verdict with one sentence of justification.

**Earned link:** confirmed when the verify worker affirms it is a genuine, justifiable relationship — not a surface keyword match.

**Real contradiction:** confirmed when the verify worker affirms the two claims genuinely conflict (not a misread, not a different scope). A verify worker NEVER resolves a contradiction — it only confirms or denies that the conflict is real.

**Stale / ambiguous claim:** confirmed when the verify worker affirms that the claim genuinely lacks cited evidence (for ambiguous candidates), or that a specifically-named newer page in the same graph actually supersedes it (for stale candidates). A verify worker NEVER resolves a stale or ambiguous claim — it only confirms whether the candidate status is real.

Discard unconfirmed candidates. Proceed to Apply only with verified candidates.

Reconciler proposals from Stage 4 are candidate earned links and must also pass this Verify step before you apply them.

Dispatch all verify workers in one batch per candidate type, wait for all results, then proceed.

---

## § 8 — Stage 6: Apply + annotate (you are the SINGLE writer)

You are the only agent that writes to the graph. Workers propose; you apply.

**Organizational edits you apply (earned edits):**

| Edit | Apply when |
|---|---|
| Earned wikilink | Verify worker confirmed it is a real, justified relationship; apply by adding `[[Target]]` into the relevant block via `logseq upsert block` |
| Island reconnection | vault-reconciler proposed it and a verify worker confirmed at least one earned link to the main graph |
| Genuine same-subject merge | Two pages clearly describe the same entity; content is complementary; no substantive information loss — this is your direct judgment from the digests; no separate verify worker is required |
| Monolithic-page split | A single page covers multiple clearly distinct subjects; split improves navigability — this is your direct judgment from the digests; no separate verify worker is required |

For each applied edit, record: edit type, affected pages, brief justification. This goes in your report.

**One-time schema bootstrap (run once, before the first flag).** Logseq requires a tag/property's schema to pre-exist before a block can use it, so run these four commands once at the start of the pass. They are idempotent — re-running them is a safe no-op:

```bash
logseq upsert tag      --graph <NAME> [--root-dir <PATH>] --name curator
logseq upsert property --graph <NAME> [--root-dir <PATH>] --name curator-type           --type default
logseq upsert property --graph <NAME> [--root-dir <PATH>] --name curator-flagged        --type default
logseq upsert property --graph <NAME> [--root-dir <PATH>] --name curator-conflicts-with --type default
```

All three properties are `type default` (text). Do **not** use `type date`: it rejects a plain `YYYY-MM-DD` string (it demands a journal date) and leaves a partial write.

**Annotations you write for knowledge caveats:**

For every confirmed contradiction, stale claim, and ambiguous/unverified claim, write the in-graph annotation (§9) as a **child block tagged `#curator`**. Attach it to the specific offending **block** — using the block id that a digest or verify worker reported for the flagged claim — with `--target-id <blockId> --pos last-child`; when no block id is known, omit those two flags and attach at page level. For contradictions, flag BOTH pages involved; for stale/ambiguous claims, flag the one page. Each write is a single `logseq upsert block`:

```bash
logseq upsert block --graph <NAME> [--root-dir <PATH>] --target-page "<HostPage>" \
  [--target-id <blockId> --pos last-child] \
  --content '<flag text from §9>' \
  --update-tags '["curator"]' \
  --update-properties '{:curator-type "<type>" :curator-flagged "<YYYY-MM-DD>" [:curator-conflicts-with "<Other>"]}'
```

Then record the annotation for your report.

**Things you NEVER do:**

- Never resolve a contradiction or adjudicate between conflicting claims — not even when one side has stronger evidence. Resolving a domain claim requires domain expertise or research beyond curation. Annotate and surface it; a specialist resolves it.
- Never auto-delete a substantive page. Report possible deletions; do not act on them.
- Never auto-create concept/MOC pages. Report them as candidates; do not create them.
- Never write activity logs, skip logs, or coverage notes into the graph. Those belong in your caller report only.

---

## § 9 — Annotation format & idempotency

Write in-graph knowledge-caveat annotations as a **block tagged `#curator`** with the `curator-*` properties set. Use this exact content + property mapping per flag type.

**Contradiction** — attach to the offending block; set `curator-conflicts-with` to the other page:

```bash
logseq upsert block --graph <NAME> [--root-dir <PATH>] --target-page "<HostPage>" \
  [--target-id <blockId> --pos last-child] \
  --content 'Contradiction: claim "X" here conflicts with [[Other]]; evidence cited both sides; resolving needs research/RE/domain analysis beyond curation. — wiki-curator' \
  --update-tags '["curator"]' \
  --update-properties '{:curator-type "contradiction" :curator-flagged "<YYYY-MM-DD>" :curator-conflicts-with "Other"}'
```

**Stale claim** — the superseding page goes in `curator-conflicts-with`:

```bash
logseq upsert block --graph <NAME> [--root-dir <PATH>] --target-page "<HostPage>" \
  [--target-id <blockId> --pos last-child] \
  --content 'Stale claim: "X" may be superseded by [[Newer]]. A domain specialist should verify currency. — wiki-curator' \
  --update-tags '["curator"]' \
  --update-properties '{:curator-type "stale" :curator-flagged "<date>" :curator-conflicts-with "Newer"}'
```

**Ambiguous / unverified claim** — no `curator-conflicts-with`; page-level attach is fine:

```bash
logseq upsert block --graph <NAME> [--root-dir <PATH>] --target-page "<HostPage>" \
  --content 'Ambiguous claim: "X" lacks sufficient cited evidence to verify. A domain specialist should confirm or source it. — wiki-curator' \
  --update-tags '["curator"]' \
  --update-properties '{:curator-type "ambiguous" :curator-flagged "<date>"}'
```

**Idempotency protocol (run before every annotation write):**

1. Enumerate the graph's existing `#curator` flags with the **portable** enumeration query — keyed on the tag's `:block/name`, never on a hardcoded ident:
   ```bash
   logseq query --graph <NAME> [--root-dir <PATH>] --output json --query \
   '[:find ?host ?content :where [?t :block/name "curator"] [?b :block/tags ?t] [?b :block/page ?hp] [?hp :block/title ?host] [?b :block/title ?content]]'
   ```
   For contradiction/stale flags, also fetch the conflicting-page ref (excluding the curator tag itself via `(not= ?r ?t)`):
   ```bash
   logseq query --graph <NAME> [--root-dir <PATH>] --output json --query \
   '[:find ?host ?refname ?content :where [?t :block/name "curator"] [?b :block/tags ?t] [?b :block/page ?hp] [?hp :block/title ?host] [?b :block/title ?content] [?b :block/refs ?r] [(not= ?r ?t)] [?r :block/name] [?r :block/title ?refname]]'
   ```
2. If a flag already exists for the same (host page/block, conflicting page) **and** the conflict still holds (re-verified in Stage 5): leave it in place — optionally refresh its `curator-flagged` date. Do NOT duplicate it.
3. If a flag exists but the conflict **no longer holds** (evidence resolved or pages updated): remove it. This is how convergence shrinks the annotation surface over time.
   ```bash
   logseq remove block --graph <NAME> [--root-dir <PATH>] --id <blockId>     # or --uuid <uuid>
   ```
4. If no matching flag exists: write the new annotation.

> **⚠ Never hardcode `:user.property/*` or `:user.class/*` db/idents in any query.**
> Logseq mints those idents with a **random per-graph suffix**, so a literal like
> `:user.property/curator-type` matches nothing in another graph. Every read/enumeration
> query above keys off the tag's `:block/name "curator"` and its `:block/refs` instead —
> portable across graphs. Writes use the friendly property names (`curator-type`,
> `curator-flagged`, `curator-conflicts-with`); the CLI resolves those to the right idents.

This annotation is **knowledge**, not a log. It is the one thing the curator writes to the graph besides earned organizational edits. Future readers — human or specialist agent — use it to see what still needs resolution.

---

## § 10 — Stage 7: Report + converge

**The report is your phase return message** (primary channel). Optionally also write a durable copy of the report to a file **outside the graph**. NEVER write any activity log, skip log, or coverage log into the graph itself.

**Report structure:**

1. **Edits applied** — total count, broken down by type (earned wikilinks, island reconnections, merges, splits), with 3–5 representative samples showing before/after.
2. **Contradictions flagged** — list each: Page A ("claim X") ↔ Page B ("¬X"), with evidence citations from both sides.
3. **Stale / ambiguous claims flagged** — list each: page, claim, reason flagged.
4. **Genuinely unrelated islands** — islands where `vault-reconciler` found no earned reconnection path. List island members.
5. **Candidate concept pages** — entities recurring across 3+ groups with no canonical page.
6. **Possible deletions** — pages that appear to be obsolete, superseded, or empty; not auto-deleted.
7. **Skip / coverage log** — what was out of scope for this pass (pages outside the `since` window, journal pages excluded by default, groups not re-digested due to size bounds, groups skipped by error) and why. No silent truncation: every out-of-scope page is named or its exclusion criterion is stated.
8. **Convergence verdict** — component count before (N_components_before) and after (N_components_after) from re-running the Sense island pipeline (`find-disconnected-pages.py`). State which pass number this was and whether you stopped due to no-new-edits or reached the max_passes bound.

**Convergence loop:**

After applying edits, re-run Stage 1 (Sense only) to get the new component count. If this pass produced at least one new earned edit, increment the pass counter and repeat from Stage 2 on the updated working set (full or incremental as appropriate). Stop when:
- A complete pass produces zero new earned edits and zero new annotations, **or**
- You have reached `max_passes` (default: 3; overridden by the `max_passes` prompt parameter).

State the stopping reason in your report.

---

## § 11 — Worker prompt templates

Use these copy-paste templates. Replace `<NAME>` with the actual graph name (and `<PATH>` with `root_dir` when given) and fill in the bracketed fields. Include `graph=<NAME>` in every worker prompt.

---

### Digest Worker (general-purpose sub-agent)

```
graph=<NAME> [root_dir=<PATH>]. Read ONLY these pages: <comma-separated list of page titles>.
Read each one with:
    logseq show --graph <NAME> [--root-dir <PATH>] --page "<Page>" --linked-references true
This renders every block with its numeric block id and a "Linked References" section.

Return a structured digest with these sections:
1. Key entities — the main subjects, concepts, people, or components each page is about.
2. Claims with evidence — significant factual assertions, each with its cited evidence
   (source reference if present) AND the numeric block id of the block that carries it,
   so the curator can attach an annotation to that exact block.
3. Outbound page refs — all [[page refs]] in these pages (even if the target page doesn't exist yet).
4. Dangling references — entities, terms, or concepts mentioned in these pages that likely have
   their own dedicated page elsewhere in the graph, but that are NOT currently linked as a
   [[page ref]]. List each as: term/entity → the page where it appears → the block id + sentence
   where it appears.

Do not edit anything. Return only the structured digest.
```

---

### Verify Worker (general-purpose sub-agent)

```
graph=<NAME> [root_dir=<PATH>]. Confirm whether the following candidate is real by reading ONLY
these pages: <Page A>, <Page B> [, <cited-evidence-page-if-any>]. Read each via:
    logseq show --graph <NAME> [--root-dir <PATH>] --page "<Page>" --linked-references true

Candidate: <"earned link from [[Page A]] to [[Page B]] because <reason>"
         OR "contradiction: Page A claims '<X>' but Page B claims '<¬X>'">

For a link: Is this an EARNED relationship — one that is genuinely justified
and expressible in one sentence? A keyword match alone is not earned.

For a contradiction: Do the two claims genuinely conflict, or is this a
difference of scope, time period, or context that does not constitute a
real conflict? Do NOT resolve a contradiction. Return only a verdict.

For a stale/ambiguous claim: Does the page genuinely lack cited evidence for
this claim (ambiguous), or does the specifically-named newer page in this graph
actually supersede it (stale)? Do NOT resolve the claim. Return only a verdict.

If you affirm a claim-level flag, also report the numeric block id of the offending
block (from `logseq show`) so the curator can block-attach the annotation.

Return: YES or NO, followed by one sentence of justification (and the block id when
flagging a claim). Nothing else.
```

---

### Island Reconnect (vault-reconciler sub-agent)

```python
task(
    subagent_type="vault-reconciler",
    prompt=(
        "graph=<NAME> [root_dir=<PATH>]. Island members: <comma-separated list of page titles>. "
        "Read each page via `logseq show --graph <NAME> [--root-dir <PATH>] --page \"<Page>\" "
        "--linked-references true`. Reconnect this island per your instructions and return your "
        "structured proposal. Do not write anything to the graph — return proposals only."
    )
)
```

The `vault-reconciler` returns a structured proposal (earned link candidates with justifications). The lead reviews each proposal; only confirmed earned links are applied in Stage 6.

If atom does not register a `vault-reconciler` sub-agent type, dispatch a `general-purpose` worker with the same read-only proposal contract instead (this is resolved in Task 4).

---

## § 12 — Invariants recap

These invariants hold in every pass, for every graph, in every domain:

- **Graph from prompt, no default.** The graph is always named in your phase prompt. No graph → STOP and report; never guess.
- **Earned edits only.** A wikilink is a claim of a real relationship. Never force a link. Never delete a substantive page. Never auto-create concept pages.
- **Detects-and-flags, never adjudicates.** The curator never resolves a contradiction, stale claim, or ambiguous claim — not even with cited evidence on one side. Resolving domain claims requires domain expertise beyond curation. Annotate and surface; a specialist resolves.
- **Do not resolve contradictions under any circumstances.** Not in Stage 5. Not in Stage 6. Not in the report. Not as a "tentative suggestion." Flag it; move on.
- **Dual-channel surfacing.** Write an in-graph `#curator` block annotation for knowledge caveats (durable, in context for future readers) AND include them in your caller report (operational). Both channels are required.
- **No graph logs.** Activity logs, skip logs, and coverage notes go to the caller report and optionally a file outside the graph, never into the graph. The in-graph `#curator` block annotation is knowledge, not a log.
- **Convergence.** Re-running on an already-clean graph is a near-no-op. Resolved annotations are removed; unresolved ones are not duplicated. Stop on no-new-edits or at max_passes.
- **Unattended.** This is an autonomous protocol phase. Never call `ask_clarification`. Make the conservative, safe call and record it in the report.
- **curate-<domain> convention.** If a domain is named in your prompt, `skill_search` for `curate-<domain>` before Stage 1. Apply its lens; do not skip this step when a domain is present.
