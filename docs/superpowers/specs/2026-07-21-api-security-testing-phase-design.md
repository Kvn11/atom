# API security & privacy testing phase — design (Steps 2 & 3)

- **Date:** 2026-07-21
- **Status:** Brainstorm design — **pending user review** (three default decisions flagged below need confirm/override)
- **Scope:** The testing phase of the `api-security-assessment` workflow, added as **Step 2 (Hypothesize)** and **Step 3 (Test)** after the existing Step 1 (Setup). Step 2 fans out per target API to write security + privacy **hypotheses** into each endpoint's vault note; Step 3 fans out per target API to **test** those hypotheses, documenting every attempt, result, and blocker. Extends the shipped `api-recon-toolkit` skill with two vault helpers. Continues on the same weak model (`gemini-3.5-flash`) and the same lead-coordinates-sub-agents pattern established in Step 1.

---

## 1. Problem & preconditions

Step 1 built a documented SDK (in the run `{{ workspace }}/sdk/`), harvested reusable values (recon note + `values.json`), and inventoried observed APIs into the domain-split Obsidian vault. Now we test the **target** APIs (from `targets.json`) for vulnerabilities.

Two kinds of finding are in scope:
1. **Traditional security** — IDOR/BOLA, mass-assignment, broken authn/authz, injection, etc.
2. **Privacy** — a narrower, explicit question: **does the API produce PII for some unauthorized user?** (i.e. can an attacker read another user's PII through this endpoint).

The user's ordering requirement is strict: **hypotheses are written to the endpoint note in the vault FIRST**; only once every target has hypotheses does testing begin. Every test attempt, result, and **blocker** must be documented — and blockers must carry stable IDs so that "blocker X was removed" maps directly to "here are all the endpoints now unblocked."

**Preconditions (assumed from Step 1, in the same run):** the SDK exists in `{{ workspace }}/sdk/`, `{{ workspace }}/recon/values.json` exists, and the vault holds recon + observed-endpoint notes. The vault (`api-security-assessment`) is registered and split by domain at the root.

---

## 2. Default decisions (pending user confirmation)

Asked during brainstorming; the user was away, so the spec adopts the **recommended** option for each. **Please confirm or override at review.**

| # | Decision | Adopted default | Alternatives |
|---|---|---|---|
| T1 | **Test posture / safety** | **Live, safe-by-default**: execute non-destructive probes live (reads + non-mutating requests) under the anti-bot rules (§6); for state-changing/destructive hypotheses (DELETE, account/data mutation, password/email change), document the exact probe but **do not send it** — mark `destructive-skipped` + `[[BLK-destructive-skipped]]` and flag for a human. | Live-execute-everything (disposable test env); dry-run-only (never send). |
| T2 | **Structure** | **Extend `api-security-assessment.yaml`** with Step 2 + Step 3, so a run does setup→hypothesize→test in one pipeline sharing the workspace (SDK + recon available); vault persists across runs. | Separate `api-security-testing.yaml` (reads vault; SDK not in its workspace). |
| T3 | **Identities for cross-user/PII tests** | **Capture-derived + blocker**: use victim IDs / second-identity material already in the recon values; when a genuine second authenticated identity is required but absent, record a `no-second-account` blocker. No new inputs. | Add optional second-identity workflow inputs. |

---

## 3. Architecture — two sequential steps, per-API fan-out

Both steps reuse Step 1's proven pattern: the task **lead is a coordinator** that delegates one **`bash` sub-agent per target endpoint** (`delegate_task subagent_type="bash"`). Sub-agents inherit the task's model (`gemini-3.5-flash`), share the run sandbox (SDK + recon reachable), and — being `bash` type — get a shell and the notes-vault instruction. Steps are sequential (Step 3 only starts once every Step-2 sub-agent has written its hypotheses to the vault).

```
Step 2 — Hypothesize (lead coordinates)          Step 3 — Test (lead coordinates)
  targets.py list --format json                    read recon values -> mint-once token, corpus UA, header set
  delegate 1 bash sub-agent / target:              delegate 1 bash sub-agent / target:
    - ensure endpoint note exists                    - read note's ## Hypotheses
    - append ## Hypotheses (security + privacy)      - test each (SDK/curl + captured creds), safe-by-default
    - report                                         - append ## Test log (attempt/result/verdict)
  summarize hypothesis counts                        - register blockers ([[BLK-<slug>]])
                                                     summarize -> {{ outputs }}/test-report.md + present_files
```

**Why per-endpoint fan-out (not a lead loop):** identical to Step 1's rationale — a lead testing 25 endpoints × several probes each in one thread would blow the ~400-super-step recursion limit; delegating keeps the lead to ~N short `delegate_task` calls and gives each child a bounded, single-endpoint job.

---

## 4. Note organization (the delegated design)

**Endpoint note** (`<domain>/endpoints/<slug>.md`) accretes sections across phases — one note per endpoint, same template family as Step 1:
```
(recon frontmatter + Request/Response/Headers/Auth/Oracle/Observations)   <- Step 1
## Hypotheses                                                             <- Step 2
  ### H1 — <category> — <one-line theory>
  - Probe: <concrete request: method, path, which identity, key params/body>
  - Privacy? <yes/no: does this expose another user's PII?>
## Test log                                                               <- Step 3
  ### H1 — <verdict: confirmed | not-vulnerable | inconclusive | blocked>
  - Attempt: <request summary + identity used + UA>
  - Result: <status + minimal response evidence (PII redacted to field names)>
  - Blocker (if blocked): [[BLK-<slug>]] — <one line>
```
Hypothesis categories always include the explicit **Privacy/PII-exposure** category alongside the security ones.

**Blocker notes** — a blocker is its **own note** at `<domain>/blockers/BLK-<slug>.md`:
```
---
id: BLK-<slug>
status: open            # open | removed
kind: blocker
---
# BLK-<slug>
<description: what's blocking, and what removing it would require>

## Affected endpoints
- [[<endpoint-slug-a>]]
- [[<endpoint-slug-b>]]
```
Endpoint test-logs reference `[[BLK-<slug>]]`; the blocker note maintains the reverse "Affected endpoints" list. **So "BLK-X removed" → open `blockers/BLK-X.md` (or its Obsidian backlinks) → every unblocked endpoint, instantly** — satisfying the core requirement. A **controlled vocabulary** of blocker slugs keeps the same real-world blocker consistently ID'd across endpoints (so removal maps cleanly):

`no-second-account` · `no-victim-id` · `auth-expired` · `waf-403` · `rate-limited` · `mfa-required` · `destructive-skipped` · `endpoint-unreachable` · `needs-write-scope`

(Ad-hoc slugs allowed when none fit; the prompt lists the vocab first.)

---

## 5. Shipped tooling additions — extend `skill_library/api-recon-toolkit/scripts/vault_note.py`

Two new subcommands (reuse `resolve_root`/`compute_slug`; keep stdlib-only). Both do a **file-locked** (`fcntl.flock`) read-modify-write so parallel sub-agents can't lose updates to a shared note.

| Subcommand | Purpose | Signature |
|---|---|---|
| `append` | Add a section to an existing endpoint note without shell-mangling. Create-if-missing. | `append (--vault N \| --root R) --domain D --slug S --from FILE` → `append_section(note_path, text) -> Path` |
| `blocker` | Idempotently create/update a blocker note and append an affected-endpoint link (deduped). | `blocker (--vault N \| --root R) --domain D --id SLUG --endpoint ENDPOINT_SLUG [--desc-from FILE] [--status open\|removed]` → `register_blocker(...) -> Path` |

`register_blocker`: creates `<root>/D/blockers/BLK-<SLUG>.md` with frontmatter (id/status) + description (from `--desc-from` on first creation) if absent; ensures a `## Affected endpoints` section; appends `- [[ENDPOINT_SLUG]]` iff not already present. `--status removed` flips the frontmatter (for when a human clears a blocker). All under `flock` on the blocker file to serialize concurrent registrations of the same blocker.

The actual HTTP requests use the Step-1 **SDK** (imported from `{{ workspace }}/sdk/`) or `curl`; no new request tool is shipped (curl/SDK + captured creds cover it, and a wrapper would just re-implement curl).

---

## 6. Testing procedure & anti-bot rules (baked into the Step-3 prompts)

A weak model doing live testing must not burn the test account (there is a documented 2026-05-08 lockout). The lead enforces **mint-once**, and every sub-agent prompt carries these rules verbatim:

1. **Mint-once token.** The lead reads the recon values and passes the working auth token/cookie to every sub-agent; sub-agents reuse it and re-mint **only** on a 401, never pre-emptively.
2. **Corpus User-Agent.** Use the UA captured in recon (mobile/webview), never `curl/*` or `python-*`.
3. **Full client header set.** Match the captured request (`Accept-Encoding/Language`, `Origin`, `Referer`, `Sec-Fetch-*`, `X-Requested-With`).
4. **Throttle + jitter.** ≤2 RPS per endpoint, ≤5 aggregate; 50–200 ms randomized inter-request delay.
5. **Bounded parallelism.** atom already clamps sub-agent concurrency to [2,4] (< the ≤10 rule).
6. **No failed-login bursts** on auth/token endpoints.
7. **Use captured identifiers**, do not enumerate ID spaces (unless the user explicitly authorizes it).
8. **Stop on lockout signal** (403/500/"No active account"/"inactive") — do not retry-loop; record a `[[BLK-auth-expired]]` / `[[BLK-rate-limited]]` blocker and report.

Combined with T1 (safe-by-default), destructive hypotheses are documented but never sent.

---

## 7. Workflow YAML additions (Steps 2 & 3)

Appended to `workflows/api-security-assessment.yaml` (both `model: gemini-3.5-flash`, `thinking: high`). Full prompt text is authored in the implementation plan; the shape:

- **Step 2 "Hypothesize"** — task `hypothesize` (lead): `targets.py list`; delegate one bash sub-agent per target with an exact prompt that (a) computes the slug, (b) ensures the endpoint note exists (read existing recon note, else stub from `targets.py show` via `put`), (c) develops security + privacy hypotheses using the note + recon values, (d) writes them to `{{ workspace }}/hyp/<slug>.md` and files them with `vault_note.py append`, (e) reports a one-liner. Lead summarizes counts.
- **Step 3 "Test"** — task `test` (lead): read `{{ workspace }}/recon/values.json` to fix the mint-once token + corpus UA + header set; delegate one bash sub-agent per target with an exact prompt embedding the anti-bot rules, the safe-by-default posture, the SDK path, and the shared creds — the sub-agent reads its note's hypotheses, tests each (safe-by-default), appends a `## Test log`, and registers blockers via `vault_note.py blocker`. Lead writes `{{ outputs }}/test-report.md` (findings by severity + a blocker table with affected-endpoint counts) and calls `present_files`.

---

## 8. Testing strategy

- **Unit tests (pytest)** for the new helpers, using a temp "vault root":
  - `append` creates a missing note and appends a section to an existing one; content is byte-exact; concurrent appends to the same note don't lose data (spawn two processes under `flock`).
  - `register_blocker` creates `blockers/BLK-<slug>.md` with frontmatter; appends `[[endpoint]]` once (idempotent on repeat); `--status removed` flips frontmatter; two concurrent registrations of the same blocker both land (flock serializes, no lost update).
- **Workflow-shape test:** the workflow now has 3 steps; Step 2 task `hypothesize`, Step 3 task `test`, both `gemini-3.5-flash`; both prompts are COORDINATOR + `delegate_task subagent_type="bash"`; Step-3 prompt contains the anti-bot rules + safe-by-default language + `vault_note.py blocker`.
- No live-model / live-HTTP test in CI (validated later via LangSmith + a real authorized run).

---

## 9. Safety & risks

- **Destructive actions** (e.g. the vesync `delAccount`) → T1 safe-by-default: documented, never sent; `destructive-skipped` blocker.
- **Account lockout** → §6 anti-bot rules + mint-once + stop-on-lockout; atom's [2,4] concurrency clamp bounds parallel load.
- **PII in notes** → record PII **by field name / presence**, not raw values (a confirmed leak says "returned victim's `email`,`phone`", not the values); reuse the redaction posture from Step 1.
- **Blocker race (parallel sub-agents)** → `flock` on the blocker note serializes concurrent registrations.
- **Weak model forgets `subagent_type="bash"`** → prompts make it loud (same residual risk as Step 1; watch in LangSmith).

---

## 10. Out of scope (roadmap)

- Re-test / regression loop after a blocker is cleared (a future step that reads `status: removed` blockers and re-delegates their affected endpoints).
- Severity scoring / a consolidated cross-domain report.
- Authorized ID-enumeration or auth-endpoint rate-limit testing (needs explicit per-run opt-in).
