# API Security Assessment — Confirm Phase + Re-run Safety (Design)

**Date:** 2026-07-21
**Feature:** A findings-confirmation phase for the `api-security-assessment` workflow, plus
re-run safety against a previously-assessed domain's persistent vault.

## Goal

After the **Test** step (Step 3) discovers 0+ vulnerabilities, make findings a first-class,
machine-checkable artifact:

1. The Test step **emits** each confirmed vulnerability as a structured JSON finding into a JSONL.
2. A new **Confirm** step (Step 4) fans out one sub-agent per finding to **independently reproduce**
   it from its recorded curl evidence. Reproduced → kept; not reproduced → discarded to an audit log.
3. The JSONL of LLM-confirmed findings is the **final deliverable**.

Additionally, because a run may target **new APIs on a previously-assessed domain**, the workflow
must not crash on, or destroy, pre-existing vault content.

As with every step, the **lead coordinates and sub-agents do the per-item work** — one finding per
sub-agent — so the lead never loops over findings in its own context and never hits the recursion limit.

## Context (existing state)

- Workflow `workflows/api-security-assessment.yaml`: **Setup → Hypothesize → Test** (all
  `gemini-3.5-flash`). Every task is a coordinator that delegates per-endpoint work to
  `subagent_type="bash"` sub-agents.
- Toolkit `skill_library/api-recon-toolkit/scripts/`: `burp.py`, `targets.py`, `vault_note.py`,
  shared helpers in `_burp.py` (incl. `find_jwts(text)->list[str]` and `redact_tokens(text)->str`).
- Persistent notes: a **named** Obsidian vault `api-security-assessment`, split by domain at the root
  (`<domain>/recon.md`, `<domain>/endpoints/<slug>.md`, `<domain>/blockers/BLK-<id>.md`).
- **Per-run** scratch dirs (fresh every run): `{{ workspace }}`, `{{ outputs }}`, `{{ uploads }}`
  (`runs/<run_id>/…`). The **vault is the only surface that persists across runs.**
- Prompt template vars available: input names (`targets`, `capture`), `inputs`, `workspace`,
  `uploads`, `outputs`, `date` (today, ISO). There is **no** `run_id` var — use `{{ date }}` for
  run-labeling.

## Locked decisions

| Topic | Decision |
|---|---|
| Evidence credentials | **Tokenless.** Evidence stores the `TOKEN=$(burp.py cred … --index N); curl -H "Authorization: $TOKEN" …` idiom, reproducible verbatim. `findings.py add` **rejects** any finding containing a raw JWT. |
| Finding schema | Core only now (a later step enriches): `title`, `description`, `evidence` (list of command strings), `confirmed` (`null`=unreviewed / `false`=failed repro / `true`=passed). |
| Non-reproducible findings | Excluded from the deliverable and appended to `discarded-findings.jsonl` with `{reason, repro_output}` (JWT-redacted). Never silently dropped. |
| No-op signaling | Every mutating toolkit CLI prints `OK:` when it changed something and `NOOP:` when it didn't, so the agent reading stdout is never misled. |
| Re-run safety | Endpoint notes are **create-if-missing** (never re-clobbered); `recon.md` **accumulates** a dated section; endpoint appended-sections are date-stamped. |

## Finding schema

One JSON object per line (JSONL). `evidence` is an ordered list of shell command strings — each a
single, self-contained line that mints its token inline and issues the request, e.g.:

```json
{
  "title": "IDOR: any authenticated user reads another user's profile",
  "description": "GET /api/v1/users/{id} returns the target user's PII regardless of the caller's identity.",
  "evidence": [
    "TOKEN=$(python3 /mnt/skill_library/api-recon-toolkit/scripts/burp.py cred /mnt/user-data/uploads/capture.xml --index 3 --field authorization); curl -sS -X GET 'https://api.example.com/api/v1/users/99902222' -H \"Authorization: $TOKEN\" -H 'User-Agent: <corpus-UA>'"
  ],
  "confirmed": null
}
```

Validation (enforced by `findings.py add`):

- `title` — non-empty string. `description` — non-empty string.
- `evidence` — non-empty list of non-empty strings.
- `confirmed` — optional; must be one of `null` / `true` / `false` (accept `0`/`1`, normalize to bool).
  Defaults to `null` on `add`.
- **No raw JWT** anywhere in the serialized finding (scan all string fields with `_burp.find_jwts`);
  reject with a non-zero exit + a stderr message naming the offending field.

## Component 1 — `findings.py` (new toolkit CLI, stdlib-only)

Lives at `skill_library/api-recon-toolkit/scripts/findings.py`. Imports `_burp` for JWT detection
and reuses `vault_note._locked_rmw`-style `fcntl.flock` append (multiple Test sub-agents append
concurrently). Missing input JSONL is treated as **empty**, never an error.

Subcommands:

- `add --from <finding.json> --to <findings.jsonl>` — validate + reject raw JWT, default
  `confirmed=null`, append one JSON line under an exclusive flock. Prints `OK: added -> <jsonl>`.
- `list <jsonl> [--format json]` — slice table: `index`, `title`, `confirmed`, `#evidence`. Never
  dumps descriptions or evidence (keeps the weak lead's context small). Missing file → 0 rows.
- `show <jsonl> --index N` — the one full finding (JSON), including its evidence commands.
- `confirm --from <raw.jsonl> --index N --to <confirmed.jsonl>` — copy finding N with
  `confirmed=true`, append (flock'd). Prints `OK: confirmed F<N> -> <confirmed.jsonl>`.
- `discard --from <raw.jsonl> --index N --to <discarded.jsonl> --reason "<why>" [--output-from <file>]`
  — copy finding N with `confirmed=false`, add `reason` and `repro_output` (the file's text, run
  through `redact_tokens`), append (flock'd). Prints `OK: discarded F<N> -> <discarded.jsonl>`.

Helpers (module-level, unit-tested directly): `validate_finding(obj) -> obj` (raises `ValueError`
with a field name on failure), `has_raw_jwt(obj) -> str | None` (returns offending field or None),
`append_jsonl(path, obj)` (flock'd), `read_jsonl(path) -> list` (missing → `[]`).

## Component 2 — `vault_note.py` changes (re-run safety + no-op signaling)

1. **`write_note(...)` returns `(Path, action)`** where `action ∈ {"wrote","skipped"}`, and gains an
   `if_missing: bool = False` param:
   - `if_missing` and target exists → **no-op**: return `(target, "skipped")` without writing.
   - else existing behavior: honor `overwrite` (raise on exists-without-overwrite), then write and
     return `(target, "wrote")`.
   - The two existing tests that assert `write_note(...) == path` are updated to unpack `(p, _)`.
2. **`cmd_put`** gains `--if-missing`; prints `NOOP: note exists, skipped -> <p>` on `"skipped"`,
   else `OK: wrote -> <p>`.
3. **`append_section(root, domain, slug, text, kind="endpoint")`** — `kind="recon"` targets
   `<domain>/recon.md` (slug ignored); `"endpoint"` unchanged. `cmd_append` gains
   `--kind {endpoint,recon}` (default `endpoint`). Prints `OK: appended -> <p>`.
4. **`register_blocker(...)` returns `(Path, action)`** where `action ∈
   {"created","updated","unchanged"}` — `"unchanged"` means the endpoint was already linked and the
   status was not flipped (a pure no-op). `cmd_blocker` prints `NOOP: [[<endpoint>]] already linked,
   no change -> <p>` on `"unchanged"`, else `OK: blocker <action> -> <p>`.

**No-op signaling convention (documented in SKILL.md):** a leading `OK:` means the write happened;
a leading `NOOP:` means it did **not** (the note already existed, or nothing changed) — the agent
must react accordingly (e.g. report *already documented (prior run)* rather than assume a fresh write).
Errors remain on stderr with a non-zero exit.

## Component 3 — workflow `api-security-assessment.yaml`

### Test step (Step 3) — emit findings + re-run safety

- Each per-endpoint sub-agent, on a **confirmed** hypothesis (esp. a privacy/PII leak), in addition to
  the vault test-log: writes the finding JSON to `{{ workspace }}/test/<slug>.<Hn>.finding.json`
  (`title`, `description`, `evidence` = the **exact tokenless curl(s)** that reproduced it), then
  `findings.py add --from … --to {{ workspace }}/findings.jsonl`. Only confirmed hypotheses become
  findings; `confirmed` stays `null`. `evidence` must be tokenless (the `add` guard enforces it).
- Re-run safety in the sub-agent prompt: the test-log append heading is date-stamped
  (`## Test log — {{ date }}`); a `NOOP:` from any vault write is reported, not treated as success.
- STEP C report notes the count of findings emitted to `{{ workspace }}/findings.jsonl`.

### Setup step (Step 1) — re-run safety

- Recon endpoint-note sub-agents use `vault_note.py put --if-missing` (not `--overwrite`); a `NOOP:`
  (note already exists from a prior assessment) is reported as *already documented (prior run)* and the
  prior note — including its `## Hypotheses` / `## Test log` sections — is left intact.
- The per-domain `recon.md` write switches from `put --kind recon --overwrite` to
  `append --kind recon` with a `## Recon — {{ date }}` heading, so each run's harvested values
  accumulate chronologically instead of overwriting the previous capture's.

### Hypothesize step (Step 2) — re-run safety

- Stub creation switches from `put --overwrite` to `put --if-missing` (a bare stub can never clobber a
  real recon note, even if the model's branch logic slips).
- The hypotheses append heading is date-stamped (`## Hypotheses — {{ date }}`) so re-assessing the same
  endpoint stacks chronologically rather than producing duplicate bare `## Hypotheses` sections.

### Confirm step (Step 4) — new

One task `confirm`, `model: gemini-3.5-flash`, `thinking: high`. Coordinator pattern:

- **STEP A (lead):** `findings.py list {{ workspace }}/findings.jsonl --format json`. **Zero findings →
  gracefully** `touch {{ outputs }}/confirmed-findings.jsonl`, write a `confirmation-summary.md`
  ("Test emitted 0 findings; nothing to confirm"), `present_files`, and stop. No delegation.
- **STEP B (fan-out):** for each finding index, `delegate_task` one `subagent_type="bash"` sub-agent.
  Each sub-agent:
  - `findings.py show {{ workspace }}/findings.jsonl --index N` to read the finding + its evidence.
  - Runs **each evidence command verbatim** — one bash call per command so the inline `$TOKEN` capture
    works (shell state does not persist between bash calls). Same anti-bot rules as Test (mint-once,
    corpus User-Agent, throttle ≤2 req/s).
  - Judges reproduction against the `description` (e.g. a privacy finding reproduces only if the
    attacker identity receives the victim's PII fields). PII recorded by **field name/presence**, never
    raw values; `reason`/`repro_output` kept to a status line + minimal evidence, not a raw body dump.
  - Reproduced → `findings.py confirm --from {{ workspace }}/findings.jsonl --index N --to
    {{ outputs }}/confirmed-findings.jsonl`. Not → write a short reason to a file, then
    `findings.py discard --from … --index N --to {{ outputs }}/discarded-findings.jsonl --reason "<why>"
    --output-from <file>`.
  - Replies one line: `F<N> -> confirmed|discarded`.
- **STEP C (lead):** `findings.py list {{ outputs }}/confirmed-findings.jsonl` for the count; write
  `{{ outputs }}/confirmation-summary.md` (N emitted / M confirmed / K discarded + reasons);
  `present_files` on **`confirmed-findings.jsonl` — the final deliverable** — and the summary.

The Confirm phase writes only to per-run `{{ workspace }}` / `{{ outputs }}`, so it is inherently
re-run-safe; no vault idempotency work is needed there.

## Testing strategy (TDD)

New `tests/test_api_recon_findings.py`:

- `add` validates + appends a valid finding; `confirmed` defaults to `null`.
- `add` rejects missing/empty `title` / `description`; rejects non-list / empty / non-string `evidence`.
- `add` **rejects a raw JWT** in evidence (uses `fx.SAMPLE_JWT`) — non-zero exit, field named.
- `add` is flock-safe under concurrent appends (two processes; both lines survive) — mirrors the
  existing blocker-concurrency test.
- `list` shows index/title/confirmed/#evidence and does **not** dump description/evidence; missing
  file → 0 rows.
- `show` returns the full finding.
- `confirm` copies finding N with `confirmed=true` to the confirmed JSONL.
- `discard` copies finding N with `confirmed=false` + `reason` + `repro_output`, and **redacts a JWT**
  in `repro_output`.

Extend `tests/test_api_recon_vault_ops.py`:

- `put --if-missing` on an existing note is a **no-op** (original body preserved) and prints `NOOP:`.
- `append --kind recon` appends to `<domain>/recon.md`.
- `register_blocker` returns `"unchanged"` when re-linking an already-linked endpoint with no status
  change; `"updated"` when a new endpoint is linked; `"created"` on first creation.

Update `tests/test_api_recon_vault_note.py`: unpack `(p, action)` from `write_note`.

Extend `tests/test_api_security_assessment_workflow.py`:

- Steps are `["Setup","Hypothesize","Test","Confirm"]`; Confirm has one task `confirm`,
  `gemini-3.5-flash`.
- Test prompt references `findings.py add` and `{{ workspace }}/findings.jsonl` and the tokenless
  evidence idiom.
- Setup recon prompt uses `put --if-missing` and `append --kind recon`; Hypothesize stub uses
  `--if-missing`.
- Confirm prompt: `COORDINATOR`, `delegate_task`, `subagent_type="bash"`, `findings.py show`,
  `findings.py confirm`, `findings.py discard`, names `confirmed-findings.jsonl` as the deliverable,
  mentions `discarded-findings.jsonl`, and instructs verbatim reproduction.

Run with `.venv/bin/python -m pytest` (conftest needs the venv's langchain_core).

## Files touched

- **Create:** `skill_library/api-recon-toolkit/scripts/findings.py`;
  `tests/test_api_recon_findings.py`.
- **Modify:** `skill_library/api-recon-toolkit/scripts/vault_note.py` (`put --if-missing`,
  `append --kind`, action-returning `write_note`/`register_blocker`, `OK:`/`NOOP:` output);
  `workflows/api-security-assessment.yaml` (Test emit + Setup/Hypothesize re-run safety + Confirm
  step); `skill_library/api-recon-toolkit/SKILL.md` (document `findings.py` + the `OK:`/`NOOP:`
  convention); `tests/test_api_recon_vault_note.py`, `tests/test_api_recon_vault_ops.py`,
  `tests/test_api_security_assessment_workflow.py`; `README.md`; the
  `atom-secassess-workflow` memory file.

## Out of scope

- Finding **enrichment** (id, endpoint, severity, category, privacy fields) — an explicitly deferred
  future step; the schema stays core (title/description/evidence/confirmed) now.
- Merging/diffing a re-observed endpoint's changed request/response shape — re-runs preserve the prior
  endpoint note as-is (create-if-missing); genuine shape drift is not reconciled.
- Deduplicating identical findings within a single run.
- Any destructive/state-changing reproduction — Confirm reproduces under the same safe-by-default rule
  as Test (destructive evidence is documented, never re-sent).
