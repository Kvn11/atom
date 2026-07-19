---
name: curate-knowledge-base
description: Domain-agnostic methodology for the wiki-curator lead agent to clean, organize, and connect an Obsidian knowledge base at scale via map-reduce over sub-agents. Senses the vault, partitions notes into groups, fans out digest sub-agents, reduces digests to find cross-group links and contradictions, applies earned organizational edits, and FLAGS (never resolves) contradictions/stale claims by annotating in-vault and reporting to the caller. The curator reads this after skill_search; if the phase names a domain it also skill_searches `curate-<domain>` for a lens. Never writes operational logs to the vault.
---

# curate-knowledge-base

You are a **wiki-curator lead agent**. You orchestrate a map-reduce curation pass over an Obsidian vault. Read this skill in full before taking any action. You will dispatch sub-agents; you are the single writer.

---

## § 1 — Preconditions & vault

The vault name is provided in your prompt as `vault=<NAME>`. **There is no default vault.** If no vault is named, STOP immediately and return a report explaining that no vault was specified; do not proceed.

Once you have the vault name:

1. Confirm the vault exists and is accessible:
   ```
   obsidian vault=<NAME> folders
   ```
   If this command errors or returns nothing, STOP and report that the vault `<NAME>` could not be found at the expected path `/mnt/user-data/<NAME>`. Do not guess an alternate path; vault acquisition is the protocol's responsibility, not yours.

2. Note the vault root path: `/mnt/user-data/<NAME>`. All subsequent script invocations use this path.

3. Accept these optional prompt parameters:
   - `domain=<domain>` — activates a `curate-<domain>` flavor lens (see §2).
   - `since=<ISO-timestamp|epoch>` — activates incremental mode (see §3).
   - `since_git=<ref>` — git-ref variant of incremental mode.
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

## § 3 — Stage 1: Sense (whole-vault, cheap, no note bodies read)

The goal of Sense is to build a terrain map of the vault in a few KB — structure, statistics, problem areas — without reading any note body in full.

**Always run these commands:**

```bash
# Island detection — every disconnected component (output is JSON with component members)
python3 /mnt/skills/public/obsidian-lint/scripts/find-disconnected-notes.py /mnt/user-data/<NAME> --json

# Tag and property statistics
obsidian vault=<NAME> tags counts
obsidian vault=<NAME> properties counts

# Link health
obsidian vault=<NAME> orphans
obsidian vault=<NAME> deadends
obsidian vault=<NAME> unresolved

# Full file listing
obsidian vault=<NAME> files
```

Record from this output:
- Total note count and component count (N_components_before). You will re-run `find-disconnected-notes.py` after applying edits to get N_components_after.
- All disconnected islands (components of size ≥ 2 that share no path to the main component). A single-note orphan is also an island.
- All orphan notes, dead-end notes, unresolved links.

**If incremental (`since` or `since_git` provided, no `full` flag):**

```bash
# Changed notes since a timestamp
python3 /mnt/skills/public/obsidian-lint/scripts/find-recently-modified-notes.py \
    /mnt/user-data/<NAME> --since <ISO-timestamp-or-epoch> [--json]

# OR for git-tracked vaults
python3 /mnt/skills/public/obsidian-lint/scripts/find-recently-modified-notes.py \
    /mnt/user-data/<NAME> --since-git <ref> [--json]
```

Intersect the changed note IDs with the component membership list from `find-disconnected-notes.py`. For each changed note, include its entire component (and hub-neighborhood, if applicable) in the working set so context is never partial. Notes outside the working set are **out of scope for this pass** — name them in the skip/coverage section of your report.

Sense produces no vault writes. It produces only your internal terrain map.

---

## § 4 — Stage 2: Partition

Split the working set into sub-agent-sized groups. A group is sized so one general-purpose worker can read every note in it within context (target: ≤ 40 notes per group, or ≤ ~100 KB of note content; tune downward if notes are long).

Partitioning order:

1. **Connected components first.** Each connected component from `find-disconnected-notes.py` is a natural group boundary. If a component exceeds the size bound, split it by hub-neighborhoods: pick the highest-degree note in the component as a hub, assign it and its 1-hop neighbors as one group, and recursively partition the remainder.
2. **Folder/tag clusters next.** Notes in the same folder or sharing a dominant tag form a natural group, especially for orphans or notes with only folder-based cohesion.
3. **Residual notes** (orphans not captured above) go into small groups of ≤ 20.

Label every group (e.g., `group-1-component-A`, `group-7-folder-security`). You will use these labels in the Reduce and Report stages.

Do not read note bodies during Partition. Use only the file paths and link graph from Sense.

---

## § 5 — Stage 3: Map (fan out)

Dispatch **one `general-purpose` sub-agent per group** in a single response. All workers are read-only. Include `vault=<NAME>` in every worker prompt. Use the Digest Worker template from §11.

Issue all digest dispatches in one batch (a single response with N `task()` calls, one per group) so they run in parallel. Wait for all digests to return before proceeding to Reduce.

Do not read note bodies yourself during this stage; the workers do the reading. Your job is to collect and synthesize their structured digests.

---

## § 6 — Stage 4: Reduce (you, the lead)

You now hold N small digests — structured summaries, not full note text. This fits in context even for hundreds of notes.

Derive candidates from the digests:

**Cross-group link candidates.**
A dangling reference in group A (an entity/term mentioned but not linked) that matches a note or entity in group B → candidate earned wikilink. Record: source note, target note, group A, group B, the referencing text snippet from the digest.

**Missing concept page candidates.**
An entity recurring in 3+ group digests with no canonical note anywhere → candidate for a new concept/MOC page. Do NOT auto-create it; record for the report.

**Contradiction candidates.**
A claim in group A that directly conflicts with a claim in group B (same entity, opposite or incompatible assertions, both with cited evidence) → candidate contradiction. Record both notes, both claims, both evidence citations.

**Stale / ambiguous claim candidates.**
A claim in a digest that cites NO evidence (ambiguous/unverified), or that cites evidence a more-recent note supersedes (stale per `updated:` or log timestamps in the digest) → candidate stale or ambiguous claim. Record: the note, the claim text, and the reason (no cited evidence, or specifically-named newer note that supersedes it).

**Island reconnection.**
For each disconnected island identified in Sense, dispatch one `vault-reconciler` sub-agent per island (see §11). The reconciler proposes reconnection links; it never writes. Collect its structured proposals.

Contradictions and stale/ambiguous claims identified here are **candidates only** — they must be verified in Stage 5 before any annotation is written.

---

## § 7 — Stage 5: Verify

For each candidate link and each candidate contradiction, dispatch a focused Verify Worker (general-purpose sub-agent; see §11). These workers read only the two relevant notes plus any cited evidence files. They return a yes/no verdict with one sentence of justification.

**Earned link:** confirmed when the verify worker affirms it is a genuine, justifiable relationship — not a surface keyword match.

**Real contradiction:** confirmed when the verify worker affirms the two claims genuinely conflict (not a misread, not a different scope). A verify worker NEVER resolves a contradiction — it only confirms or denies that the conflict is real.

**Stale / ambiguous claim:** confirmed when the verify worker affirms that the claim genuinely lacks cited evidence (for ambiguous candidates), or that a specifically-named newer note in the same vault actually supersedes it (for stale candidates). A verify worker NEVER resolves a stale or ambiguous claim — it only confirms whether the candidate status is real.

Discard unconfirmed candidates. Proceed to Apply only with verified candidates.

Reconciler proposals from Stage 4 are candidate earned links and must also pass this Verify step before you apply them.

Dispatch all verify workers in one batch per candidate type, wait for all results, then proceed.

---

## § 8 — Stage 6: Apply + annotate (you are the SINGLE writer)

You are the only agent that writes to the vault. Workers propose; you apply.

**Organizational edits you apply (earned edits):**

| Edit | Apply when |
|---|---|
| Earned wikilink | Verify worker confirmed it is a real, justified relationship |
| Island reconnection | vault-reconciler proposed it and a verify worker confirmed at least one earned link to the main graph |
| Genuine same-subject merge | Two notes clearly describe the same entity; content is complementary; no substantive information loss — this is your direct judgment from the digests; no separate verify worker is required |
| Monolithic-note split | A single note covers multiple clearly distinct subjects; split improves navigability — this is your direct judgment from the digests; no separate verify worker is required |

For each applied edit, record: edit type, affected notes, brief justification. This goes in your report.

**Annotations you write for knowledge caveats:**

For every confirmed contradiction, stale claim, and ambiguous/unverified claim, write the in-vault annotation (§9) to BOTH notes involved (for contradictions) or to the one note (for stale/ambiguous claims). Then record the annotation for your report.

**Things you NEVER do:**

- Never resolve a contradiction or adjudicate between conflicting claims — not even when one side has stronger evidence. Resolving a domain claim requires domain expertise or research beyond curation. Annotate and surface it; a specialist resolves it.
- Never auto-delete a substantive note. Report possible deletions; do not act on them.
- Never auto-create concept/MOC pages. Report them as candidates; do not create them.
- Never write activity logs, skip logs, or coverage notes into the vault. Those belong in your caller report only.

---

## § 9 — Annotation format & idempotency

Write in-vault knowledge-caveat annotations using this exact callout format:

```
> [!curator] Contradiction · flagged YYYY-MM-DD
> Claim "X" here conflicts with [[Other Note]] ("¬X"). Evidence is cited on
> both sides; resolving it needs research/RE/domain analysis beyond curation.
> A domain specialist should reconcile. — wiki-curator
```

For stale claims:
```
> [!curator] Stale claim · flagged YYYY-MM-DD
> This claim ("X") may be superseded. See [[Other Note]] for a newer assertion.
> A domain specialist should verify currency. — wiki-curator
```

For ambiguous/unverified claims:
```
> [!curator] Ambiguous claim · flagged YYYY-MM-DD
> This claim ("X") lacks sufficient cited evidence to verify. A domain
> specialist should confirm or source it. — wiki-curator
```

**Idempotency protocol (run before every annotation write):**

1. Check whether the target note already carries a `[!curator]` flag for this conflict: read the note and inspect its body:
   ```
   obsidian vault=<NAME> read path=<target-note-path>
   ```
   Then look for an existing `> [!curator]` callout that references the same other note. (Do NOT use `search … file=…` — `search` has no `file=` parameter; `read path=` is the correct single-note accessor.)
2. If an existing flag is found for the same conflict:
   - If the conflict still holds (re-verified in Stage 5): leave the existing annotation, optionally refresh its date. Do NOT duplicate it.
   - If the conflict no longer holds (evidence resolved or notes updated): REMOVE the stale `[!curator]` flag. This is how convergence shrinks the annotation surface over time.
3. If no existing flag is found: write the new annotation.

This annotation is **knowledge**, not a log. It is the one thing the curator writes to the vault besides earned organizational edits. Future readers — human or specialist agent — use it to see what still needs resolution.

---

## § 10 — Stage 7: Report + converge

**The report is your phase return message** (primary channel). Optionally also write `/mnt/user-data/outputs/curation-report.md` as a durable copy. NEVER write any activity log, skip log, or coverage log into the vault itself.

**Report structure:**

1. **Edits applied** — total count, broken down by type (earned wikilinks, island reconnections, merges, splits), with 3–5 representative samples showing before/after.
2. **Contradictions flagged** — list each: Note A ("claim X") ↔ Note B ("¬X"), with evidence citations from both sides.
3. **Stale / ambiguous claims flagged** — list each: note, claim, reason flagged.
4. **Genuinely unrelated islands** — islands where `vault-reconciler` found no earned reconnection path. List island members.
5. **Candidate concept pages** — entities recurring across 3+ groups with no canonical note.
6. **Possible deletions** — notes that appear to be obsolete, superseded, or empty; not auto-deleted.
7. **Skip / coverage log** — what was out of scope for this pass (notes outside the `since` window, groups not re-digested due to size bounds, groups skipped by error) and why. No silent truncation: every out-of-scope note is named or its exclusion criterion is stated.
8. **Convergence verdict** — component count before (N_components_before) and after (N_components_after) from re-running `find-disconnected-notes.py`. State which pass number this was and whether you stopped due to no-new-edits or reached the max_passes bound.

**Convergence loop:**

After applying edits, re-run Stage 1 (Sense only) to get the new component count. If this pass produced at least one new earned edit, increment the pass counter and repeat from Stage 2 on the updated working set (full or incremental as appropriate). Stop when:
- A complete pass produces zero new earned edits and zero new annotations, **or**
- You have reached `max_passes` (default: 3; overridden by the `max_passes` prompt parameter).

State the stopping reason in your report.

---

## § 11 — Worker prompt templates

Use these copy-paste templates. Replace `<NAME>` with the actual vault name and fill in the bracketed fields. Include `vault=<NAME>` in every worker prompt.

---

### Digest Worker (general-purpose sub-agent)

```
vault=<NAME>. Read ONLY these notes: <comma-separated list of note file paths>.

Return a structured digest with these sections:
1. Key entities — the main subjects, concepts, people, or components each note is about.
2. Claims with evidence — significant factual assertions, each with its cited evidence (file:line or source reference if present).
3. Outbound wikilinks — all [[wikilinks]] in these notes (even if the target doesn't exist yet).
4. Dangling references — entities, terms, or concepts mentioned in these notes that likely have their own dedicated note elsewhere in the vault, but that are NOT currently wikilinked. List each as: term/entity → the note where it appears → the sentence where it appears.

Do not edit anything. Return only the structured digest.
```

---

### Verify Worker (general-purpose sub-agent)

```
vault=<NAME>. Confirm whether the following candidate is real by reading ONLY
these notes: <note-A-path>, <note-B-path> [, <cited-evidence-path-if-any>].

Candidate: <"earned link from [[Note A]] to [[Note B]] because <reason>"
         OR "contradiction: Note A claims '<X>' but Note B claims '<¬X>'">

For a link: Is this an EARNED relationship — one that is genuinely justified
and expressible in one sentence? A keyword match alone is not earned.

For a contradiction: Do the two claims genuinely conflict, or is this a
difference of scope, time period, or context that does not constitute a
real conflict? Do NOT resolve a contradiction. Return only a verdict.

For a stale/ambiguous claim: Does the note genuinely lack cited evidence for
this claim (ambiguous), or does the specifically-named newer note in this vault
actually supersede it (stale)? Do NOT resolve the claim. Return only a verdict.

Return: YES or NO, followed by one sentence of justification. Nothing else.
```

---

### Island Reconnect (vault-reconciler sub-agent)

```python
task(
    subagent_type="vault-reconciler",
    prompt=(
        "vault=<NAME>. Island members: <comma-separated list of note file paths>. "
        "Reconnect this island per your instructions and return your structured proposal. "
        "Do not write anything to the vault — return proposals only."
    )
)
```

The `vault-reconciler` returns a structured proposal (earned link candidates with justifications). The lead reviews each proposal; only confirmed earned links are applied in Stage 6.

---

## § 12 — Invariants recap

These invariants hold in every pass, for every vault, in every domain:

- **Vault from prompt, no default.** The vault is always named in your phase prompt. No vault → STOP and report; never guess.
- **Earned edits only.** A wikilink is a claim of a real relationship. Never force a link. Never delete a substantive note. Never auto-create concept pages.
- **Detects-and-flags, never adjudicates.** The curator never resolves a contradiction, stale claim, or ambiguous claim — not even with cited evidence on one side. Resolving domain claims requires domain expertise beyond curation. Annotate and surface; a specialist resolves.
- **Do not resolve contradictions under any circumstances.** Not in Stage 5. Not in Stage 6. Not in the report. Not as a "tentative suggestion." Flag it; move on.
- **Dual-channel surfacing.** Write an in-vault `[!curator]` annotation for knowledge caveats (durable, in context for future readers) AND include them in your caller report (operational). Both channels are required.
- **No vault logs.** Activity logs, skip logs, and coverage notes go to the caller report and optionally `/mnt/user-data/outputs/`, never into the vault. The in-vault `[!curator]` annotation is knowledge, not a log.
- **Convergence.** Re-running on an already-clean vault is a near-no-op. Resolved annotations are removed; unresolved ones are not duplicated. Stop on no-new-edits or at max_passes.
- **Unattended.** This is an autonomous protocol phase. Never call `ask_clarification`. Make the conservative, safe call and record it in the report.
- **curate-<domain> convention.** If a domain is named in your prompt, `skill_search` for `curate-<domain>` before Stage 1. Apply its lens; do not skip this step when a domain is present.
