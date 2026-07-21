# API security & privacy assessment workflow — design (Step 1: Setup)

- **Date:** 2026-07-21
- **Status:** Approved + implemented (merged to `main`). See §12 for the post-merge revision.
- **Scope:** A production atom workflow that performs an authorized security & privacy assessment of a customer's *own* API targets, driven by a **weak reasoning model**. This spec fully specifies **Step 1 (Setup)** and the **shipped CLI tooling** it depends on; later steps (deep per-endpoint threat analysis, live testing, reporting) are sketched as roadmap only. The workflow ships in-repo (`workflows/` + `skill_library/`) and is pushed to the remote; the user copies it into `$HOME/.atom/` themselves.

> **Revision 2026-07-21b (see §12):** the model is now **Gemini 3.5 Flash** (`gemini-3.5-flash`, reasoning via `thinking_level`), and **D3 flipped to sub-agent fan-out** — the lead coordinates and delegates per-endpoint work to **bash sub-agents** rather than looping itself. Read §12 alongside the original text below, which it supersedes on those two points.

---

## 1. Problem & inputs

Given two files describing an authorized assessment of APIs the org owns, set up everything a (weak) model needs before it starts probing:

1. **`targets` (file, required)** — a JSON file naming the **scope**: up to **25 primary API endpoints under one domain**, each with a `method`, `path`, and pseudo-schema `request`/`response` example. These are what we ultimately test. Example (`examples/targets.json`):
   ```json
   { "domain": "my.api.com",
     "api": [ { "method": "POST", "path": "/some/api/*/?x={}",
                "request": "{'foo':'int' #optional,'bar':'str' #required}",
                "response": "{'zoo':'int'}" } ] }
   ```
2. **`capture` (file, required)** — a **Burp Suite XML export** of live traffic (`examples/account.vesync.com.xml`, ~1 MB, 23 items). Each `<item>` is a base64-encoded raw HTTP request+response with real headers, cookies, tokens, and IDs. Its purpose is to supply **reusable, real values** for testing the targets (e.g. a real account id `22134806` and `terminalId` embedded in a JWT `authorizeCode`, session cookies, auth-header shapes) **and** to inventory every API actually observed in the wild.

**Why this is hard / why tooling matters:** real captures are *mostly noise* — the example is 22 static JS/CSS/SVG assets and only 1 real API request. Bodies can be tens of KB (full HTML/JS). Target request/response schemas can be very long, ×25. A weak model cannot hold all of this at once and cannot be trusted to discover skills or hand-quote large shell strings. So the design leans on **purpose-built, LLM-friendly CLI tooling that slices instead of dumps**, and **prompts that spell out an explicit per-endpoint loop**.

---

## 2. Platform facts this design relies on (verified in code)

These were confirmed by reading the atom source; they are the load-bearing constraints.

1. **Every workflow task is a full lead agent.** A task can override only `model` and `thinking` (`workflow/schema.py` `TaskDef`). There is **no per-task `tools`/`skills`/`profile`**. → We set `model: gemini-pro` + `thinking:` **per task**, so the workflow is self-contained (no `config.yaml`/`--profile` requirement). `gemini-pro` is already registered → `gemini-2.5-pro` (1M ctx, reasoning). Gemini 3 is **not** registered and must not be used (it refuses security work).
2. **`{{ workspace }}` is shared across all tasks and all steps** (`runs/<id>/workspace`, existing-mode bind); **`{{ outputs }}` is per-task-isolated** (keyed by thread id). → Anything a later step/task must reuse (the SDK, the extracted values) goes to **`{{ workspace }}`**; only final deliverables go to `{{ outputs }}` + `present_files`.
3. **Bash rewrites virtual `/mnt/...` paths to real host paths before running** (`sandbox/provider.py::_rewrite_virtual`), cwd = workspace. So a task can run `python3 /mnt/skill_library/api-recon-toolkit/scripts/burp.py view /mnt/user-data/uploads/capture.xml --index 5 --keys` and every path resolves. `/mnt/skill_library` → `$ATOM_HOME/skill_library`; `/mnt/user-data/uploads/<name>` → the run's upload.
4. **Bash env is scrubbed of all API keys** (`GOOGLE_API_KEY`, `*_API_KEY`, `*TOKEN*`, …). → Step 1 does **no authenticated live requests** (it is recon + SDK build); live testing is a later, gated step that gets its credentials from captured values, not host env.
5. **Uploaded file inputs** land at `/mnt/user-data/uploads/<input-name><ext>` and are readable by both `read_file` and bash/scripts.
6. **Notes vault:** atom validates a **pre-registered** Obsidian vault (never creates it) and injects an `obsidian vault=<name> …` instruction block into every task's system prompt. Nested folders appear **implicitly** from a `path=` that includes them (no mkdir). The CLI's `create/append` take only **inline `content=`**, which **mangles large markdown** (shell re-interpretation) — a known pitfall. The vault's on-disk path is discoverable via `obsidian vault=<name> vault info=path`.
7. **Shipped scripts are not on `PATH`** and skills are **not reliably auto-discovered by a weak model.** → We invoke scripts by **absolute `/mnt/skill_library/...` path from prompts**, and put the real guidance **in the task prompts** (the `SKILL.md` is a secondary reference, not the primary driver).
8. **`$ATOM_HOME/skill_library` is currently empty** and the repo's `skill_library/` is the source of truth the user copies from. → Deployment ships **two** artifacts (workflow YAML + skill dir); documented in the YAML header.

---

## 3. Default decisions (pending user confirmation)

These were asked during brainstorming; the user was away, so the spec adopts the **recommended** option for each. **Please confirm or override at review** — each changes the code.

| # | Decision | Adopted default | Alternatives |
|---|---|---|---|
| D1 | **Targets file parsing** | **Tolerant loader**: try strict JSON first; on failure normalize (strip `//`/`#`-line comments, remove trailing commas, insert missing commas between adjacent members) and warn. Also fix `examples/targets.json` to round-trip. | Strict-only (fix example); strict-then-fallback (essentially the adopted behavior). |
| D2 | **SDK form** | **Documented Python `httpx` client** in `{{ workspace }}/sdk/` — one function per target endpoint, typed params, docstrings, base-URL + auth placeholders; plus a README. | Curl/httpie recipe pack; TypeScript client. |
| D3 | **Weak-model scale strategy** | **Sequential loop + slicing CLI**: one agent iterates endpoint-by-endpoint, viewing only that endpoint's sliced data, writing its note, then moving on. The CLI does the context management. | Sub-agent fan-out via `delegate_task`; defer per-endpoint notes to a dedicated later step. |
| D4 | **Step-1 note depth** | **Recon inventory only**: request shape, response shape, headers, auth scheme, **oracle?**, observations. No vuln analysis yet (that's a later testing step). | Recon + light threat hints; full AppSec threat template now. |

---

## 4. Architecture overview

```
INPUTS (uploads)                 STEP 1 — "Setup" (2 parallel tasks)              PERSISTENT VAULT (per domain)
  targets.json  ───────────────▶ [build_sdk]  gemini-pro                         <domain>/
  capture.xml   ──┐              reads targets via targets.py                       recon.md          (reusable values)
                  │              writes {{workspace}}/sdk/*.py + README             endpoints/
                  └────────────▶ [capture_recon] gemini-pro                          get_root.md
                                 reads capture via burp.py                            post_api_v1_users.md
                                 harvests reusable values, loops endpoints            ...
                                 writes vault notes + {{workspace}}/recon/values.json

SHIPPED SKILL (repo skill_library/ → ~/.atom/skill_library/)
  api-recon-toolkit/scripts/{_burp.py, burp.py, targets.py, vault_note.py}
```

- **Two parallel tasks in one step**, exactly as requested. They read *different* inputs (recon ← capture; sdk ← targets), so parallelism is clean — no shared-state dependency.
- **The SDK built in Step 1 is structural** (endpoints/params/shapes from `targets.json`). Wiring in real auth/headers/IDs harvested by `capture_recon` is a **later step** (they run in parallel here, so recon output isn't available to `build_sdk` yet). *(Assumption — flag if you want SDK-build sequenced after recon instead.)*

---

## 5. Shipped tooling — `skill_library/api-recon-toolkit/`

All scripts are **Python 3 stdlib-only** (portable to the target device), invoked as `python3 /mnt/skill_library/api-recon-toolkit/scripts/<script>.py …`. They **truncate by default** and expose flags to view narrow slices.

### 5.1 `_burp.py` — shared parser (ported from `analyze-burp-requests`)
Reuses the proven, already-verified logic: base64 + gzip/deflate body decode, `HttpMessage` (headers/cookies/content-type/`is_json`/`body_json`/`body_text`), `iter_items(xml)`, `json_keys_summary`, `truncate_text`, `safe_path_component`. Additions: `decode_jwt(token)` (header/payload/sig-byte-length, **never the raw token**), `endpoint_slug(method, path)`.

### 5.2 `burp.py` — capture inspector (the "slice, don't dump" CLI)
| Subcommand | Purpose |
|---|---|
| `hosts <xml>` | Distinct hosts/domains + item counts (captures can span domains). |
| `index <xml> [--apis-only] [--method M] [--status S] [--url-contains T] [--format json]` | Compact item table. **`--apis-only`** hides static assets (by extension *and* by mimetype: `script/css/image/font`), so the model sees signal not noise. |
| `view <xml> --index N [--summary\|--req-headers\|--resp-headers\|--cookies\|--req-body\|--resp-body] [--keys] [--limit N] [--decode-auth]` | Single-item inspector. `--keys` = JSON key-tree only (safe for huge bodies); `--limit` truncates; `--decode-auth` finds+decodes JWTs in Authorization header / cookies / query params. |
| `harvest <xml> [--format json]` | Sweep the whole capture → **reusable artifacts**: distinct request headers (name→sample, truncated), cookies (name→origin), bearer/JWT shapes + decoded claims (`sub`/`aud`/`user_id`/`exp`), and candidate identifiers (ints/uuids/hex) that recur across requests. This *is* the "reusable headers/identifiers/values/cookies" deliverable. |

### 5.3 `targets.py` — targets-file inspector
| Subcommand | Purpose |
|---|---|
| `list <targets.json> [--format json]` | Domain + count + table of `index / method / path` (+ flags: has-request-schema, has-response-schema). **Never dumps schemas.** |
| `show <targets.json> --index N [--part request\|response\|both] [--limit N]` | One target's method/path + the requested schema slice, truncated with a note when long. |

Tolerant loader (D1): `load_targets()` tries `json.loads`; on failure applies a normalization pass and re-parses, printing a one-line warning naming what it fixed; hard error (with the offending text) only if still unparseable.

### 5.4 `vault_note.py` — the note-filing helper (removes shell-quoting risk)
The model writes a note body to `{{ workspace }}` with the clean `write_file` tool, then files it with one command — **no large markdown ever passes through a shell string**.
| Subcommand | Purpose |
|---|---|
| `slug "<METHOD> <path>"` | Print the canonical slug, e.g. `post_api_v1_users` (lowercase, path only, non-alnum→`_`, collapse repeats, drop query string). Guarantees capture-observed and target notes for the same endpoint share a filename → later steps *update* one note instead of duplicating. |
| `put --vault <name> --domain <d> --slug <s> --from <workspace.md> [--kind endpoint\|recon] [--overwrite]` | Resolves the vault root via `obsidian vault=<name> vault info=path`, then writes `<root>/<d>/endpoints/<s>.md` (or `<root>/<d>/recon.md` for `--kind recon`), creating folders. Obsidian indexes the new file live. |

### 5.5 `SKILL.md`
A concise cheat-sheet of the above commands + the note templates (§6). Present so `search_skills`/`load_skill` and a curious human both work — but prompts don't depend on the model finding it.

---

## 6. Vault structure & note templates

```
<vault-root>/                       # a pre-registered Obsidian vault (name = notes.vault)
  account.vesync.com/               # ONE FOLDER PER DOMAIN, at root
    recon.md                        # reusable values/headers/cookies/IDs/oracles for this domain
    endpoints/
      get_root.md
      post_api_v1_users.md
      ...
  my.api.com/
    recon.md
    endpoints/ …
```

**Endpoint note** (`<domain>/endpoints/<slug>.md`) — **one template for both capture-observed and target endpoints** (per requirement):
```markdown
---
endpoint: <METHOD> <path>
domain: <host>
auth: <Bearer JWT (alg=HS256) | session cookie | query authorizeCode | none>
oracle: <yes|no|unknown>            # does it confirm validity/existence of some data?
source: <capture #idx | targets #idx | both>
status: <observed | target | tested>
tags: [api-recon]
---

# <METHOD> <path>

## Request shape
<observed/target body or query schema; key tree, not full dump>

## Response shape
<status class + response key tree; ⚠ mark PII-looking keys>

## Headers
<notable request/response headers; auth header shape — NOT raw token>

## Auth
<scheme; for JWT: decoded header+payload claims (sub/aud/user_id/exp), sig bytes — never raw token>

## Oracle
<yes/no/unknown + one line: which input it confirms (e.g. "reveals whether an email is registered")>

## Observations
<anything useful: linked endpoints, IDs reusable elsewhere, quirks, rate-limit hints>
```

**Recon note** (`<domain>/recon.md`) — the reusable-values catalog `harvest` feeds: base URL(s), reusable request headers, cookies, JWT/bearer shapes + decoded claims, candidate identifiers (real IDs usable as required fields when testing targets), and cross-endpoint oracles. Secrets are documented by **shape, not raw value** (captured tokens are expired/loggable).

---

## 7. Workflow YAML — `workflows/api-security-assessment.yaml`

```yaml
# workflows/api-security-assessment.yaml — copy to $ATOM_HOME/workflows/ to run it.
# ALSO copy skill_library/api-recon-toolkit/ -> $ATOM_HOME/skill_library/ (ships the CLI tooling),
# and register an Obsidian vault named "api-security-assessment" (Open folder as vault).
name: api-security-assessment
description: Authorized security & privacy assessment of your own API targets — Step 1 sets up recon + an SDK.
notes:
  enabled: true
  vault: api-security-assessment      # split by domain at the root; shared across all runs
inputs:
  - name: targets
    type: file
    required: true
    description: JSON file scoping the primary API targets (one domain, up to 25 endpoints).
  - name: capture
    type: file
    required: true
    description: Burp Suite XML capture of live traffic for reusable values + observed-API inventory.
steps:
  - title: Setup
    description: In parallel — harvest reusable values + inventory observed APIs, and build the target SDK.
    tasks:
      - id: capture_recon
        model: gemini-pro
        thinking: high        # 24576 budget — careful recon on a weak model
        prompt: |
          <explicit, step-by-step prompt: run burp.py hosts/index --apis-only/harvest;
           write recon.md via write_file then vault_note.py put --kind recon;
           THEN loop each observed API endpoint: view sliced req/resp/headers/--decode-auth,
           write endpoint note via write_file, file it with vault_note.py put; ONE endpoint at a time;
           finally summarize to {{ outputs }} + present_files.>
      - id: build_sdk
        model: gemini-pro
        thinking: medium
        prompt: |
          <explicit prompt: targets.py list; loop each target with show --part …;
           build documented httpx client in {{ workspace }}/sdk/ + README;
           present the SDK to {{ outputs }} + present_files.>
```

Prompts are the primary control surface for the weak model: exact command templates, an explicit "repeat for each endpoint" loop, and the note templates inlined. (Full prompt text is authored in the implementation plan.)

---

## 8. Testing strategy

- **Unit tests (pytest, mirroring `tests/`)** for every script — this is where correctness is actually pinned, since CI has no Gemini:
  - `burp.py index --apis-only` hides the 22 vesync assets, keeps the 1 API item; `--format json` shape.
  - `burp.py view --keys` / `--decode-auth` on the vesync JWT (`aud=22134806`) — decodes claims, never prints the raw token.
  - `burp.py harvest` surfaces the account id + cookies from the example.
  - `targets.py` tolerant-loads the **unfixed** example (missing comma) and the fixed one; `show --part` slices.
  - `vault_note.py slug` cases (`POST /api/v1/users` → `post_api_v1_users`, query strings dropped); `put` writes under a temp "vault root" at `<domain>/endpoints/<slug>.md`.
- **Workflow validity:** `WorkflowDef.model_validate(load_workflow(...))` loads; inputs/notes/steps parse (mirror `test_workflow_schema.py`).
- **Manual/LangSmith validation of the prompts** is the user's explicit next phase ("connect it to langsmith and refine the prompting") — out of scope for this spec's automated tests.

---

## 9. Packaging, deployment, remote

- **In repo:** `workflows/api-security-assessment.yaml` and `skill_library/api-recon-toolkit/{SKILL.md,scripts/*}`. Fix `examples/targets.json` to round-trip (D1).
- **User deploys:** copy the YAML → `~/.atom/workflows/`; copy the skill dir → `~/.atom/skill_library/`; register an Obsidian vault named `api-security-assessment`. (All stated in the YAML header comment + README note.)
- **Remote:** commit on a feature branch and push (per request). User promotes to `~/.atom/` themselves.

---

## 10. Risks & mitigations

- **Weak model wanders / dumps huge data** → CLI truncates by default; prompts prescribe exact sliced commands and a one-endpoint-at-a-time loop; per-task timeout 1800s (raise if 25 endpoints run long).
- **Shell-quoting mangles vault notes** → notes are written with `write_file` then filed by `vault_note.py`; no large markdown crosses a shell boundary.
- **Capture is all noise** → `harvest` still extracts cookies/JWT from the lone HTML doc; recon task reports "no XHR/API items observed" plainly.
- **Multi-domain captures** → notes route by each item's own host; the vault accumulates domains across runs (org owns them all).
- **Tolerant parser masking a real malformed file** → it warns and prints what it changed; strict attempt runs first.

---

## 10a. Refinements made during implementation

- **Token redaction at display sinks.** `burp.py` redacts JWT-shaped substrings (keeping a 12-char
  header prefix + `…<JWT redacted>`) in every output path — URLs, request-line, header/cookie values,
  and body text — so a raw bearer/authorizeCode token never leaks into the notes a weak model writes.
  Extraction (`decode-auth`, `harvest`) still runs on the raw data, so claims (`aud`, `exp`, …) and
  identifiers are fully available; only the raw token string is withheld.
- **Committed, self-contained test fixtures.** `examples/` is gitignored (local-only, possibly
  sensitive real captures), so the unit tests build a tiny synthetic Burp capture + targets file
  in-process (`tests/_secassess_fixtures.py`) rather than depending on `examples/`. The tooling was
  additionally smoke-verified against the real `examples/account.vesync.com.xml` locally.

## 12. Revision 2026-07-21b — model swap + sub-agent fan-out

Two changes landed after the initial merge, both driven by user direction:

**(a) Model → Gemini 3.5 Flash.** Gemini 3.5 Flash (`gemini-3.5-flash`, released 2026-05-19) is now
authorized for security work and is reasoning-capable, so it replaces `gemini-pro` (2.5-pro) on both
tasks. It was added to `atom.models.registry` (1M window, reasoning). **API nuance:** Gemini 3+
replaced the integer `thinking_budget` with a `thinking_level` enum (`minimal/low/medium/high`);
`thinking_budget` is deprecated for those models. `_thinking_overrides` now emits `thinking_level` for
Gemini 3+ (`thinking: high` → `{"thinking_level": "high"}`) while 2.5 models keep `thinking_budget`.
Verified against `langchain-google-genai==4.2.6` (both params exist; `thinking_level` wins on 3+).

**(b) D3 flipped to sub-agent fan-out.** The lead must NOT inspect individual endpoints itself — a
sequential loop over up-to-25 endpoints (each with several `view` calls and large outputs) exceeds the
lead's ~400-super-step recursion limit. Instead each lead is now a **coordinator**: it maps/harvests
(recon) or lists (SDK), then **delegates one `bash` sub-agent per endpoint** via `delegate_task`, and
finally summarizes/assembles. Key mechanics that make this correct (verified in `atom.subagent`):
sub-agents inherit the **task's exact model instance** (so they also run on Gemini 3.5 Flash), they
**share the parent's cached sandbox** (same run workspace/uploads, and `/mnt/skill_library` is mounted),
and **only `subagent_type="bash"` children get a shell AND the notes-vault instruction** — so the
delegation prompts require `subagent_type="bash"`. Each child handles exactly one endpoint (well under
its own 300-super-step limit), and the lead issues ~N short `delegate_task` calls instead of ~6N
inspection calls, keeping it far under its limit. Recon sub-agents write+file the endpoint note; SDK
sub-agents each write one `sdk/endpoints/<slug>.py` module (distinct files → no write races), and the
lead assembles the README + `__init__`.

## 11. Out of scope now (roadmap for later steps)

Step 1 only. Sketched for continuity (each is its own future spec):
- **Step 2 — Enrich & correlate:** wire harvested auth/headers/real IDs into the SDK; cross-map target endpoints ↔ observed endpoints; flag oracles.
- **Step 3 — Per-endpoint threat analysis:** fan out deep AppSec analysis (port the `analyze-burp-requests` threat taxonomy: IDOR/BOLA, mass-assignment, authn/JWT, input-validation, concrete probes) into per-endpoint test plans.
- **Step 4 — Live testing (gated):** authorized real requests with a test account, honoring anti-bot/lockout rules.
- **Step 5 — Report:** synthesize findings + severity.
```
