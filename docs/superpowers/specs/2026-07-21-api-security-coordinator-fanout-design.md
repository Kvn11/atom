# Design: thin the api-security-assessment coordinators so leads fan out instead of doing per-endpoint prep

Date: 2026-07-21
Status: approved (design)
Related: `2026-07-21-api-security-testing-phase-design.md`, `2026-07-21-api-security-confirm-phase-design.md`

## Problem

A production run of the `api-security-assessment` workflow's **Test** step hit the lead task's
recursion limit. The lead agent (a weak reasoning model, `gemini-3.5-flash`) did not fan out enough
work: the trace showed repeated activity for only **one** delegated endpoint — roughly 22 lead model
calls + 21 lead tool calls before/around delegation, then only ~10 sub-agent calls. The lead spent
many turns doing **endpoint-level prep in its own context**: reading target/vault data, checking
capture/header extraction, computing slugs, and picking per-endpoint identities. That work belongs in
sub-agents.

### Root cause

1. **The workflow engine has no native dynamic fan-out.** `workflow/schema.py:TaskDef` is static
   (`prompt` / `model` / `thinking` only) — there is no `for_each`/map primitive. So each step's lead
   is a `delegate_task` **coordinator** that must discover the endpoint list at runtime and delegate
   one sub-agent per item. This pattern is required; the fix is to make the coordinator genuinely thin.

2. **The Test step's `STEP A` forces per-endpoint reasoning into the lead.** It has the lead enumerate
   identities *and* compute a per-endpoint `<IDENTITY_INDEX>` (attacker) + `<VICTIM_ID>` + `<UA>`
   mapping, which it substitutes into every sub-agent prompt. To fill those placeholders the weak lead
   reads `values.json`, reasons about which identity attacks which, and inspects endpoints — burning
   its ~36-turn budget (400 super-steps ÷ ~11 per turn) before it ever fans out.

3. **No single-batch discipline.** Nothing tells the lead to emit *all* `delegate_task` calls in one
   turn. A weak model drifts into a delegate-one → inspect → delegate-next loop, so lead super-step
   cost scales with endpoint count instead of staying ~constant.

### Budget facts (from the code)

- Lead / workflow-task budget: `AgentProfile.recursion_limit = 400` super-steps ≈ 36 model turns
  (middleware chain ≈ 11 super-steps/turn). Resolved in `runtime.build_run_config` from
  `prof.recursion_limit`.
- Sub-agent budget: `SubagentConfig.recursion_limit = 300` ≈ 27 turns — its own graph invocation, so a
  sub-agent's steps do **not** count against the lead's 400.
- Concurrency: `SubagentConfig.max_concurrent` clamped to `[2,4]`. Firing all N `delegate_task` calls
  in one turn is safe — the runtime queues them and runs ≤4 at a time.

The key lever: when the lead emits all delegations in a **single assistant turn**, the lead's
super-step cost is roughly constant regardless of endpoint count, because the tool-call fan-out is one
turn's tool node — not N turns.

## Goals

- The lead does only: (A) one/two endpoint-independent listing calls, (B) fan out in one batch, (C)
  summarize. No per-endpoint reasoning, ever.
- Push all per-endpoint prep (slug, note read, identity/attacker/victim selection, header/UA
  extraction, testing) into the sub-agents, which each handle exactly one item under their own budget.
- Add a per-task recursion-limit backstop so an imperfectly-batched lead degrades gracefully instead of
  hard-failing.
- Apply the hardening consistently across **all** coordinator tasks, not just Test.

## Non-goals

- No new workflow-engine fan-out primitive (`for_each`). Deferred; larger architectural change.
- No change to the sub-agent budget (300) — sub-agents were not the failure.
- No change to the toolkit CLIs' behavior. The lead writes `identities.json` via plain shell
  redirection of the existing `burp.py identities --format json`; the JSON already carries
  `source_indices` / `user_ids` / `user_agents` for a sub-agent to self-select.

## Design

### 1. Shared "Coordinator Contract" block (all 5 lead prompts)

Inject a uniform block into `capture_recon`, `build_sdk`, `hypothesize`, `test`, `confirm`:

```
COORDINATOR CONTRACT (read first):
- You do ONLY three things: (STEP A) one or two endpoint-independent listing calls, (STEP B) fan out,
  (STEP C) summarize. Nothing else.
- Fan out ALL items in ONE message: emit every delegate_task call together in a single turn, then STOP
  and wait for their replies. NEVER delegate one item, inspect its result, then delegate the next.
- Every delegate_task MUST set subagent_type="bash" (a general-purpose sub-agent has no shell and will
  fail).
- Do NOT inspect a single endpoint/finding yourself. These are SUB-AGENT-ONLY commands you must NEVER
  run: burp.py view / cred / decode-auth / per-item identities, targets.py show, vault_note.py slug,
  obsidian read, or any per-item probe. If you catch yourself about to inspect one item, STOP and
  delegate it instead.
- After fan-out, do nothing per-item: read only the one-line replies and write the summary.
```

The literal strings `COORDINATOR` and `Do NOT inspect` are preserved (existing tests pin them).

### 2. Test step redesign (the heavy change)

**Lead `STEP A` — 2 cheap, endpoint-independent calls (no reasoning):**
- `targets.py list {{ targets }} --format json` → domain `<HOST>` (constant; targets is one domain) +
  the endpoint indices.
- `burp.py identities {{ capture }} --format json > {{ workspace }}/recon/identities.json`
  (plain redirect; no toolkit change).

The lead no longer parses `values.json` and no longer computes any attacker/victim mapping.

**Lead `STEP B` — one batch:** fire one `delegate_task` per index in a single message, substituting
only `<INDEX>` (and `<HOST>`, the constant domain).

**Sub-agent prompt — self-contained; self-selects identities:**
- Anti-bot rules are **inlined as literal text** in the sub-agent template (no `<RULES>` injection by
  the lead).
- The sub-agent reads `{{ workspace }}/recon/identities.json` and self-selects:
  - **attacker** = an identity's `source_indices[0]` (used with `burp.py cred --index …`),
  - **victim** = a *different* identity's `user_ids[0]`; if only one identity exists, register a
    `no-victim-id` blocker and mark cross-user tests blocked,
  - **UA** = the attacker identity's `user_agents[0]`.
- Everything else is unchanged: `targets.py show`/`slug`/`obsidian read` for shape+hypotheses, the
  `TOKEN=$(burp.py cred … --field authorization); curl …` tokenless idiom, `--field cookie` minting,
  per-hypothesis result logging, `findings.py add` emission, blocker registration, one-line reply.

Placeholders drop from five reasoning-bearing values to just `<INDEX>` (+ constant `<HOST>`).

### 3. Other coordinators (consistency hardening)

- **`hypothesize`**, **`build_sdk`**: already thin (`STEP A` = `targets.py list`; sub-agent takes only
  `<INDEX>`, `<HOST>`). Add the Contract block + single-batch language. Bodies unchanged.
- **`confirm`**: `STEP A` = `findings.py list`; sub-agent takes only `<N>`. Add the Contract block +
  single-batch language; inline its anti-bot rules literally (drop `<RULES>` injection). Keep the
  zero-findings graceful path, the SAFETY GATE, and all `findings.py` commands.
- **`capture_recon`**: slim the per-endpoint sub-agent to take **only `<INDEX>`** and derive
  method/path/host from its own `burp.py view` output (drop the `<METHOD>/<PATH>/<HOST>`
  substitutions). Keep the one-time per-domain recon (`hosts`, `index`, `harvest`, write
  `<domain>.recon.md`) in the lead — it is per-domain (typically one), not per-endpoint, and is genuine
  lead-level synthesis.

### 4. Per-task `recursion_limit` backstop

Surgical, mirroring how `model` / `thinking` already flow per-task:
- `workflow/schema.py:TaskDef` — add `recursion_limit: Optional[int] = None`.
- `runtime.run_agent` — add `override_recursion_limit: int | None = None`; compute
  `limit = override_recursion_limit or prof.recursion_limit` and pass `limit` to `build_run_config`.
- `workflow/engine.py` (the `run_agent(...)` call, ~line 466) — pass
  `override_recursion_limit=td.recursion_limit`.
- YAML — set `recursion_limit: 600` (~54 turns) on the coordinator tasks (`capture_recon`, `build_sdk`,
  `hypothesize`, `test`, `confirm`). The global `AgentProfile.recursion_limit` default stays **400**
  (keeps `test_recursion_limit.py::test_agent_profile_recursion_limit_default` green).

Rationale for 600: a thin coordinator should need only a handful of turns; 600 gives headroom for the
25-endpoint case even if the model batches imperfectly (e.g. one delegation per turn ≈ 25 turns + setup
+ summary), without masking a broken fan-out (loop detection remains the real runaway guard).

### 5. Tests

Extend `tests/test_api_security_assessment_workflow.py`:
- Every lead prompt contains the Contract markers and single-batch language ("in ONE message" / "single
  turn" / batch wording).
- The Test sub-agent block contains **no** `<IDENTITY_INDEX>` / `<VICTIM_ID>` placeholders and instructs
  self-selection from `identities.json` (assert `identities.json` + `source_indices` + `user_ids`
  appear; assert the old placeholders are absent).
- Anti-bot rules appear literally in the Test and Confirm sub-agent prompts (no `<RULES>` token).
- `capture_recon` sub-agent takes only `<INDEX>` (assert no `<METHOD>`/`<PATH>` substitution tokens in
  its delegated sub-prompt).
- Preserve all currently-pinned substrings: `delegate_task`, `subagent_type="bash"`, `COORDINATOR`,
  `Do NOT inspect`, `burp.py identities`, `TOKEN=$(`, `--field authorization`, `--field cookie`,
  `findings.py list/show/confirm/discard`, `## Test log — {{ date }}`, `## Hypotheses`, `--if-missing`,
  `## Recon — {{ date }}`, etc.

Add schema/runtime tests:
- `tests/test_workflow_schema.py` — `TaskDef(recursion_limit=…)` accepted; defaults to `None`.
- `tests/test_recursion_limit.py` — `run_agent`/`build_run_config` uses `override_recursion_limit` when
  set and falls back to `prof.recursion_limit` when `None`; the workflow's coordinator tasks declare
  `recursion_limit == 600`.

## Files touched

- `workflows/api-security-assessment.yaml` — 5 lead prompts reworked; `recursion_limit: 600` on
  coordinator tasks.
- `src/atom/workflow/schema.py` — `TaskDef.recursion_limit`.
- `src/atom/runtime.py` — `run_agent(override_recursion_limit=…)` → `build_run_config`.
- `src/atom/workflow/engine.py` — thread `td.recursion_limit` into `run_agent`.
- `tests/test_api_security_assessment_workflow.py`, `tests/test_workflow_schema.py`,
  `tests/test_recursion_limit.py` — new/updated assertions.

No toolkit script changes. `examples/` stays gitignored; tests use the synthetic fixtures in
`tests/_secassess_fixtures.py`.

## Risks / open questions

- A weak model may still not batch perfectly. Mitigations: explicit single-batch instruction + the 600
  backstop + concurrency queueing. The primary success metric is the lead's turn count dropping and all
  endpoints getting delegated — validated in the LangSmith prompt-validation phase.
- Sub-agent identity self-selection adds a little sub-agent reasoning. Acceptable: it runs under the
  sub-agent's own 300 budget, in parallel, and is more correct per-endpoint (some endpoints need no
  victim). A deterministic `burp.py identities --pair` helper is a possible future simplification but is
  out of scope here.
