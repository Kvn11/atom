# API-Security Coordinator Fan-Out Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `api-security-assessment` workflow's weak-model lead agents thin coordinators that fan out all per-endpoint work to sub-agents in one batch, so the Test step (and its siblings) stops burning its recursion budget on per-endpoint prep.

**Architecture:** The workflow engine has no native fan-out, so each step's lead is a `delegate_task` coordinator. We (1) inject a shared "Coordinator Contract" + single-batch fan-out discipline into all five lead prompts, (2) redesign the Test lead to build an identity roster once to a file so its sub-agents self-select attacker/victim, (3) slim the `capture_recon` sub-agent to derive method/path/host itself, and (4) add a per-task `recursion_limit` override (set to 600 on the coordinators) as a backstop.

**Tech Stack:** Python 3, pydantic (`atom.config.schema`, `atom.workflow.schema`), LangGraph (`recursion_limit` in `runtime.build_run_config`), Jinja-templated YAML prompts, pytest.

## Global Constraints

- Model for every task is `gemini-3.5-flash` (a weak reasoning model) — do not change it.
- Every `delegate_task` in every lead prompt MUST set `subagent_type="bash"` (general-purpose sub-agents have no shell and cannot run the toolkit).
- No toolkit script changes (`skill_library/api-recon-toolkit/scripts/*` stay as-is). The lead writes `identities.json` via plain shell redirection of the existing `burp.py identities --format json`.
- `examples/` is gitignored; tests use synthetic fixtures in `tests/_secassess_fixtures.py`. Never read real captures in tests.
- The global default `AgentProfile.recursion_limit` stays **400** and `SubagentConfig.recursion_limit` stays **300** — only add a per-task override; do not change the defaults (keeps `tests/test_recursion_limit.py::test_agent_profile_recursion_limit_default` green).
- Preserve every substring the existing tests pin: `delegate_task`, `subagent_type="bash"`, `COORDINATOR`, `Do NOT inspect`, `burp.py identities`, `TOKEN=$(`, `--field authorization`, `--field cookie`, `findings.py list`/`show`/`confirm`/`discard`, `confirmed-findings.jsonl`, `discarded-findings.jsonl`, `## Test log — {{ date }}`, `## Hypotheses`, `## Hypotheses — {{ date }}`, `## Recon — {{ date }}`, `--if-missing`, `--kind recon`, `vault_note.py append`, `SAFETY GATE`, `not re-sent`, `verbatim`, `{{ workspace }}/sdk/`, `{{ workspace }}/findings.jsonl`.
- YAML block-scalar indentation in `workflows/api-security-assessment.yaml`: lead-level lines under `prompt: |` are indented **10 spaces**; the delegated sub-agent block (everything you instruct the sub-agent to do) is indented **12 spaces** (2 deeper), which visually sets it off in the rendered prompt. Preserve this relative indentation exactly.

## File Structure

- `src/atom/workflow/schema.py` — `TaskDef` gains an optional `recursion_limit` field.
- `src/atom/runtime.py` — `run_agent` gains an `override_recursion_limit` param that wins over `prof.recursion_limit`.
- `src/atom/workflow/engine.py` — threads `td.recursion_limit` into `run_agent`.
- `workflows/api-security-assessment.yaml` — five lead prompts reworked; `recursion_limit: 600` on the five coordinator tasks.
- `tests/test_workflow_schema.py` — `TaskDef.recursion_limit` field test.
- `tests/test_recursion_limit.py` — override-wins / default-fallback tests.
- `tests/test_api_security_assessment_workflow.py` — new prompt-shape assertions.

---

### Task 1: Per-task `recursion_limit` override (schema → runtime → engine)

**Files:**
- Modify: `src/atom/workflow/schema.py:35-40` (`TaskDef`)
- Modify: `src/atom/runtime.py:84-104` (`run_agent` signature) and `src/atom/runtime.py:148` (limit resolution)
- Modify: `src/atom/workflow/engine.py:465-467` (`run_agent(...)` call)
- Test: `tests/test_workflow_schema.py`, `tests/test_recursion_limit.py`

**Interfaces:**
- Produces: `TaskDef.recursion_limit: Optional[int]` (default `None`); `run_agent(..., override_recursion_limit: int | None = None)` which resolves the effective limit as `override_recursion_limit if override_recursion_limit is not None else prof.recursion_limit`.
- Consumes: existing `build_run_config(thread_id, recursion_limit, trace=None, obs_provider=None)`.

- [ ] **Step 1: Write the failing schema test**

Add to `tests/test_workflow_schema.py` (bottom of file):

```python
def test_taskdef_recursion_limit_defaults_none_and_accepts_int():
    assert TaskDef(prompt="x").recursion_limit is None
    assert TaskDef(prompt="x", recursion_limit=600).recursion_limit == 600
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_workflow_schema.py::test_taskdef_recursion_limit_defaults_none_and_accepts_int -v`
Expected: FAIL — `TypeError`/validation error: `TaskDef` has no field `recursion_limit`.

- [ ] **Step 3: Add the field to `TaskDef`**

In `src/atom/workflow/schema.py`, the current class is:

```python
class TaskDef(_Base):
    id: Optional[str] = None
    prompt: str
    model: Optional[str] = None
    thinking: Optional[Union[str, int]] = None
```

Add one line so it becomes:

```python
class TaskDef(_Base):
    id: Optional[str] = None
    prompt: str
    model: Optional[str] = None
    thinking: Optional[Union[str, int]] = None
    # Optional per-task override of the lead recursion_limit (LangGraph super-steps). None -> use the
    # agent profile's recursion_limit. Fan-out coordinators set this high as a backstop.
    recursion_limit: Optional[int] = None
```

(`Optional` and `Union` are already imported at the top of this file.)

- [ ] **Step 4: Run the schema test to verify it passes**

Run: `python -m pytest tests/test_workflow_schema.py::test_taskdef_recursion_limit_defaults_none_and_accepts_int -v`
Expected: PASS.

- [ ] **Step 5: Write the failing runtime tests**

Add to `tests/test_recursion_limit.py` (bottom of file). These drive the real `run_agent` with a scripted model and spy on `build_run_config` to capture the effective limit:

```python
import pytest
from langchain_core.messages import AIMessage

from tests.conftest import make_prepared


def _spy_build_run_config(monkeypatch, seen):
    from atom import runtime
    real = runtime.build_run_config

    def spy(thread_id, recursion_limit, trace=None, obs_provider=None):
        seen["limit"] = recursion_limit
        return real(thread_id, recursion_limit, trace, obs_provider)

    monkeypatch.setattr(runtime, "build_run_config", spy)


@pytest.mark.asyncio
async def test_run_agent_honors_override_recursion_limit(base_config, monkeypatch):
    from atom import runtime

    seen: dict = {}
    _spy_build_run_config(monkeypatch, seen)
    prepared = make_prepared([AIMessage(content="ok")])
    await runtime.run_agent(
        "hi", config=base_config, prepared=prepared, override_recursion_limit=777
    )
    assert seen["limit"] == 777


@pytest.mark.asyncio
async def test_run_agent_defaults_to_profile_recursion_limit(base_config, monkeypatch):
    from atom import runtime

    seen: dict = {}
    _spy_build_run_config(monkeypatch, seen)
    prepared = make_prepared([AIMessage(content="ok")])
    await runtime.run_agent("hi", config=base_config, prepared=prepared)
    assert seen["limit"] == base_config.profile("default").recursion_limit
```

- [ ] **Step 6: Run them to verify they fail**

Run: `python -m pytest tests/test_recursion_limit.py -k "run_agent" -v`
Expected: FAIL — `test_run_agent_honors_override_recursion_limit` errors because `run_agent()` has no `override_recursion_limit` kwarg.

- [ ] **Step 7: Add the param to `run_agent` and resolve the limit**

In `src/atom/runtime.py`, add the keyword-only param in the `run_agent` signature (right after `override_thinking`, line 95):

```python
    override_thinking: str | int | None = None,
    override_recursion_limit: int | None = None,
```

Then change the limit line (currently line 148):

```python
        run_config = build_run_config(thread_id, prof.recursion_limit, trace, obs_provider)
```

to:

```python
        limit = (
            override_recursion_limit if override_recursion_limit is not None
            else prof.recursion_limit
        )
        run_config = build_run_config(thread_id, limit, trace, obs_provider)
```

- [ ] **Step 8: Thread the override from the engine**

In `src/atom/workflow/engine.py`, the `run_agent(...)` call (starts at line 465) passes `override_model=td.model, override_thinking=td.thinking,`. Add the recursion override on that same line:

```python
                override_model=td.model, override_thinking=td.thinking,
                override_recursion_limit=td.recursion_limit,
```

- [ ] **Step 9: Run the runtime tests to verify they pass**

Run: `python -m pytest tests/test_recursion_limit.py -v`
Expected: PASS (all cases, including the pre-existing default tests).

- [ ] **Step 10: Commit**

```bash
git add src/atom/workflow/schema.py src/atom/runtime.py src/atom/workflow/engine.py tests/test_workflow_schema.py tests/test_recursion_limit.py
git commit -m "feat(workflow): per-task recursion_limit override threaded to run_agent"
```

---

### Task 2: Raise the five coordinator tasks to `recursion_limit: 600`

**Files:**
- Modify: `workflows/api-security-assessment.yaml` (each of the five tasks: `capture_recon`, `build_sdk`, `hypothesize`, `test`, `confirm`)
- Test: `tests/test_api_security_assessment_workflow.py`

**Interfaces:**
- Consumes: `TaskDef.recursion_limit` (Task 1).
- Produces: nothing new for later tasks.

- [ ] **Step 1: Add helper + failing test**

Add to `tests/test_api_security_assessment_workflow.py`. First a small helper near the top (after the existing `_load`-style setup — this file loads the workflow via `WorkflowDef.model_validate(yaml.safe_load(WF.read_text()))`; reuse whatever loader the file already defines, referred to below as `_load()`):

```python
def _all_tasks():
    return [t for s in _load().steps for t in s.tasks]


def test_coordinator_tasks_raise_recursion_limit():
    for t in _all_tasks():
        assert t.recursion_limit == 600
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_api_security_assessment_workflow.py::test_coordinator_tasks_raise_recursion_limit -v`
Expected: FAIL — `recursion_limit` is `None` for every task.

- [ ] **Step 3: Add `recursion_limit: 600` to all five tasks**

In `workflows/api-security-assessment.yaml`, each task currently looks like:

```yaml
      - id: capture_recon
        model: gemini-3.5-flash
        thinking: high
        prompt: |
```

Insert `recursion_limit: 600` between `thinking:` and `prompt:` for **all five** tasks (`capture_recon`, `build_sdk`, `hypothesize`, `test`, `confirm`), e.g.:

```yaml
      - id: capture_recon
        model: gemini-3.5-flash
        thinking: high
        recursion_limit: 600
        prompt: |
```

(`build_sdk` uses `thinking: medium` — keep that; just add the `recursion_limit: 600` line under it.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_api_security_assessment_workflow.py::test_coordinator_tasks_raise_recursion_limit -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add workflows/api-security-assessment.yaml tests/test_api_security_assessment_workflow.py
git commit -m "feat(secassess): recursion_limit 600 backstop on all coordinator tasks"
```

---

### Task 3: Coordinator Contract + single-batch on `build_sdk`, `hypothesize`, `confirm`

**Files:**
- Modify: `workflows/api-security-assessment.yaml` (`build_sdk`, `hypothesize`, `confirm` prompts)
- Test: `tests/test_api_security_assessment_workflow.py`

**Interfaces:**
- Produces: each of these three lead prompts contains `COORDINATOR CONTRACT`, `in ONE message`, `Do NOT` … `inspect`/`reproduce`, and (for `confirm`) inlined anti-bot rules with no `<RULES>` token.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_api_security_assessment_workflow.py`:

```python
def _task(step_idx, task_idx=0):
    return _load().steps[step_idx].tasks[task_idx].prompt


def test_build_sdk_and_hypothesize_are_thin_coordinators():
    build_sdk = _task(0, 1)   # Setup step, second task
    hypothesize = _task(1, 0)  # Hypothesize step
    for p in (build_sdk, hypothesize):
        assert "COORDINATOR CONTRACT" in p
        assert "in ONE message" in p          # single-turn fan-out discipline
        assert "Do NOT inspect" in p
        assert 'subagent_type="bash"' in p


def test_confirm_is_thin_and_inlines_antibot():
    p = _task(3, 0)  # Confirm step
    assert "COORDINATOR CONTRACT" in p
    assert "in ONE message" in p
    assert "<RULES>" not in p                 # anti-bot rules are literal, not lead-injected
    assert "mint-once" in p                   # the rules appear literally
    # preserved gates from the confirm phase
    assert "SAFETY GATE" in p and "verbatim" in p.lower()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_api_security_assessment_workflow.py -k "thin_coordinator or thin_and_inlines" -v`
Expected: FAIL — `COORDINATOR CONTRACT` / `in ONE message` not present; `<RULES>` still present in confirm.

- [ ] **Step 3: Edit `build_sdk`**

In `build_sdk`, replace the coordinator paragraph (currently):

```
          YOU ARE A COORDINATOR. Do NOT inspect individual targets yourself — writing every endpoint's
          method in your own context risks the recursion limit. Authoring each endpoint's SDK function is
          SUB-AGENT work (STEP B). Your job is: list, delegate, assemble.
```

with the Contract block (10-space indent):

```
          COORDINATOR CONTRACT (read this first — it governs everything below):
          - Your job is ONLY: list the scope, fan out one bash sub-agent per target endpoint, then
            assemble. You NEVER author an endpoint's SDK function yourself.
          - Fan out ALL endpoints in ONE message: emit EVERY delegate_task call together in a single
            turn, then STOP and wait for their replies. NEVER delegate one endpoint, look at its
            result, then delegate the next — that serial loop is what exhausts your step budget.
          - Every delegate_task MUST set subagent_type="bash" (a general-purpose sub-agent has no
            shell and cannot run the toolkit — it will fail).
          - Do NOT inspect a single endpoint yourself. These are SUB-AGENT-ONLY and you must NEVER run
            them: targets.py show, vault_note.py slug, or any per-endpoint file write. If you catch
            yourself about to inspect ONE endpoint, STOP and delegate it instead.
          - After fan-out, do nothing per-endpoint: read only the sub-agents' one-line replies, then
            do STEP C (assemble).
```

Then replace the STEP B intro (currently):

```
          STEP B — DELEGATE one sub-agent PER target endpoint. For EACH index, call delegate_task with
          subagent_type="bash" (REQUIRED — a general-purpose sub-agent has no shell and will fail). You may
          issue several delegate_task calls together to run in parallel. Give each sub-agent this EXACT
          prompt, substituting <INDEX>:
```

with:

```
          STEP B — fan out (ONE message, all at once): for EACH index from STEP A, call delegate_task,
          ALL in a single turn, subagent_type="bash" (REQUIRED — a general-purpose sub-agent has no
          shell and will fail). The ONLY value that varies is <INDEX>. Give each sub-agent this EXACT
          prompt, substituting <INDEX>:
```

- [ ] **Step 4: Edit `hypothesize`**

Replace the coordinator paragraph (currently):

```
          YOU ARE A COORDINATOR. Do NOT analyze endpoints yourself — that is SUB-AGENT work and looping
          over many endpoints will hit the recursion limit. Your job: list, delegate, summarize.
```

with:

```
          COORDINATOR CONTRACT (read this first — it governs everything below):
          - Your job is ONLY: list the targets, fan out one bash sub-agent per target endpoint, then
            summarize. You NEVER analyze an endpoint yourself.
          - Fan out ALL endpoints in ONE message: emit EVERY delegate_task call together in a single
            turn, then STOP and wait for their replies. NEVER delegate one endpoint, look at its
            result, then delegate the next — that serial loop is what exhausts your step budget.
          - Every delegate_task MUST set subagent_type="bash" (a general-purpose sub-agent has no
            shell and cannot run the toolkit — it will fail).
          - Do NOT inspect a single endpoint yourself. These are SUB-AGENT-ONLY and you must NEVER run
            them: targets.py show, vault_note.py slug, obsidian read, or any per-endpoint note write. If
            you catch yourself about to inspect ONE endpoint, STOP and delegate it instead.
          - After fan-out, do nothing per-endpoint: read only the sub-agents' one-line replies, then
            write the STEP C summary.
```

Then replace the STEP B intro (currently):

```
          STEP B — DELEGATE one sub-agent PER target endpoint. For EACH index, call delegate_task with
          subagent_type="bash" (REQUIRED — a general-purpose sub-agent has no shell and will fail). You
          may issue several together to run in parallel. Give each sub-agent this EXACT prompt,
          substituting <INDEX> and <HOST>:
```

with:

```
          STEP B — fan out (ONE message, all at once): for EACH index from STEP A, call delegate_task,
          ALL in a single turn, subagent_type="bash" (REQUIRED — a general-purpose sub-agent has no
          shell and will fail). The values that vary are <INDEX> (and <HOST>, identical for all). Give
          each sub-agent this EXACT prompt, substituting <INDEX> and <HOST>:
```

- [ ] **Step 5: Edit `confirm`**

Replace the coordinator paragraph (currently):

```
          YOU ARE A COORDINATOR. Do NOT reproduce findings yourself — delegate each to a sub-agent.
          Looping over findings in your own context will hit the recursion limit.
```

with:

```
          COORDINATOR CONTRACT (read this first — it governs everything below):
          - Your job is ONLY: list the findings, fan out one bash sub-agent per finding, then assemble
            the deliverable. You NEVER reproduce a finding yourself.
          - Fan out ALL findings in ONE message: emit EVERY delegate_task call together in a single
            turn, then STOP and wait for their replies. NEVER delegate one finding, look at its result,
            then delegate the next — that serial loop is what exhausts your step budget.
          - Every delegate_task MUST set subagent_type="bash" (a general-purpose sub-agent has no
            shell and cannot run the toolkit — it will fail).
          - Do NOT reproduce a finding yourself. Running findings.py show or the evidence curl commands
            is SUB-AGENT-ONLY. If you catch yourself about to reproduce ONE finding, STOP and delegate
            it instead. (findings.py list is corpus-wide — that one you DO run in STEP A.)
          - After fan-out, do nothing per-finding: read only the sub-agents' one-line replies, then do
            STEP C (assemble).
```

Then, in the confirm SAFETY/ANTI-BOT lead lines, remove the "Copy these rules into every sub-agent prompt." sentence. The current text is:

```
          ANTI-BOT: mint-once (reuse the captured token; re-mint only on a 401), corpus User-Agent,
          throttle <=2 req/s. Copy these rules into every sub-agent prompt.
```

Replace with:

```
          ANTI-BOT: the sub-agent block below already contains these rules literally — you do not
          inject them.
```

Then replace the STEP B intro (currently):

```
          STEP B — call delegate_task to run one sub-agent PER finding index (subagent_type="bash",
          REQUIRED — a general-purpose sub-agent has no shell and will fail). You may run several in
          parallel (atom caps concurrency at 4). Give each this EXACT prompt, substituting <N> and the
          ANTI-BOT rules text:
```

with:

```
          STEP B — fan out (ONE message, all at once): call delegate_task once per finding index, ALL
          in a single turn, subagent_type="bash" (REQUIRED — a general-purpose sub-agent has no shell
          and will fail). The ONLY value that varies is <N>. Give each this EXACT prompt, substituting
          <N>:
```

Finally, in the confirm **sub-agent block**, replace the line (12-space indent):

```
            Raw findings: {{ workspace }}/findings.jsonl. Anti-bot rules: <RULES>.
```

with:

```
            Raw findings: {{ workspace }}/findings.jsonl.
            ANTI-BOT RULES (obey all): mint-once (reuse the captured token; re-mint only on a 401),
            corpus User-Agent (never curl/python defaults), throttle <=2 req/s.
```

- [ ] **Step 6: Run the new + affected existing tests**

Run: `python -m pytest tests/test_api_security_assessment_workflow.py -k "thin_coordinator or thin_and_inlines or confirm_prompt or hypothesize_prompt or leads_delegate" -v`
Expected: PASS (new tests pass; `test_confirm_prompt_reproduces_and_gates`, `test_hypothesize_prompt_delegates_and_covers_privacy`, and `test_leads_delegate_per_endpoint_to_bash_subagents` still pass — `COORDINATOR` and `Do NOT inspect` are preserved by the Contract).

- [ ] **Step 7: Commit**

```bash
git add workflows/api-security-assessment.yaml tests/test_api_security_assessment_workflow.py
git commit -m "feat(secassess): Coordinator Contract + single-batch fan-out on build_sdk/hypothesize/confirm"
```

---

### Task 4: Test step — identity roster to a file; sub-agent self-selects

**Files:**
- Modify: `workflows/api-security-assessment.yaml` (`test` prompt — full rework)
- Test: `tests/test_api_security_assessment_workflow.py`

**Interfaces:**
- Produces: the Test lead does only `targets.py list` + `burp.py identities > identities.json`, fans out one sub-agent per `<INDEX>` in one batch; the sub-agent reads `{{ workspace }}/recon/identities.json` and self-selects attacker/victim/UA.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_api_security_assessment_workflow.py`:

```python
def test_test_lead_builds_roster_file_and_subagent_self_selects():
    p = _task(2, 0)  # Test step
    assert "COORDINATOR CONTRACT" in p and "in ONE message" in p
    # lead writes the roster ONCE to a file (endpoint-independent) — still uses burp.py identities
    assert "burp.py identities" in p and "identities.json" in p
    # lead no longer computes a per-endpoint attacker/victim mapping
    assert "You will pass the relevant identities" not in p
    assert "<RULES>" not in p                      # anti-bot rules are literal in the sub-agent block
    # the sub-agent self-selects from the roster
    assert "Pick your identities from the roster" in p
    assert "source_indices" in p and "user_ids" in p
    # preserved: tokenless idiom + cookie minting + findings emission + safety
    assert "TOKEN=$(" in p and "--field authorization" in p and "--field cookie" in p
    assert "findings.py add" in p and "{{ workspace }}/findings.jsonl" in p
    assert "## Test log — {{ date }}" in p
    assert "destructive-skipped" in p and "safe-by-default" in p.lower()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_api_security_assessment_workflow.py::test_test_lead_builds_roster_file_and_subagent_self_selects -v`
Expected: FAIL — `COORDINATOR CONTRACT` / `identities.json` / `Pick your identities from the roster` not present; `You will pass the relevant identities` still present.

- [ ] **Step 3: Replace the entire `test` task prompt**

Replace the whole `prompt: |` body of the `test` task with the following (10-space lead indent; the delegated sub-agent block is 12-space indent):

```
          You are the LEAD coordinating AUTHORIZED live testing of your org's own API targets.
          Targets: {{ targets }}. Capture: {{ capture }}. SDK: {{ workspace }}/sdk/. Toolkit at
          /mnt/skill_library/api-recon-toolkit/scripts/. Vault: api-security-assessment.

          COORDINATOR CONTRACT (read this first — it governs everything below):
          - Your job is ONLY: run the two endpoint-independent setup calls, fan out one bash sub-agent
            per target endpoint, then report. You NEVER test or prep a single endpoint yourself.
          - Fan out ALL endpoints in ONE message: emit EVERY delegate_task call together in a single
            turn, then STOP and wait for their replies. NEVER delegate one endpoint, look at its
            result, then delegate the next — that serial loop is what exhausts your step budget.
          - Every delegate_task MUST set subagent_type="bash" (a general-purpose sub-agent has no
            shell and cannot run the toolkit — it will fail).
          - Do NOT inspect a single endpoint yourself. These are SUB-AGENT-ONLY and you must NEVER run
            them: burp.py view, burp.py cred, burp.py decode-auth, targets.py show, vault_note.py slug,
            obsidian read, or any per-endpoint probe or note write. Do NOT open identities.json or pick
            attacker/victim identities — each sub-agent does that itself. If you catch yourself about to
            inspect ONE endpoint, STOP and delegate it instead.
          - After fan-out, do nothing per-endpoint: read only the sub-agents' one-line replies, then
            write the STEP C report.

          SAFETY (safe-by-default): only NON-DESTRUCTIVE probes may be sent (reads / non-mutating). For
          any state-changing/destructive hypothesis (DELETE, account/data mutation, password/email
          change), the sub-agent DOCUMENTS the exact probe but DOES NOT SEND it — marked
          destructive-skipped.

          STEP A — two endpoint-independent calls (you do this; NO per-endpoint reasoning):
            python3 /mnt/skill_library/api-recon-toolkit/scripts/targets.py list {{ targets }} --format json
          Keep the domain (call it <HOST>) and the list of endpoint indices — nothing else.
            mkdir -p {{ workspace }}/recon && python3 /mnt/skill_library/api-recon-toolkit/scripts/burp.py identities {{ capture }} --format json > {{ workspace }}/recon/identities.json
          That writes the identity roster (every in-scope test account, each with source_indices /
          user_ids / user_agents) to a file the sub-agents read themselves. Do NOT open it and do NOT
          pick attacker/victim identities — each sub-agent selects its own from the roster.

          STEP B — fan out (ONE message, all at once): call delegate_task once per index from STEP A,
          ALL in a single turn, subagent_type="bash" (REQUIRED). The ONLY value that varies between the
          calls is <INDEX>; <HOST> is identical for every one. Give each sub-agent this EXACT prompt,
          substituting <INDEX> and <HOST>:

            You are testing the hypotheses for ONE target API endpoint. Do only this one. Authorized,
            live, SAFE-BY-DEFAULT — never send destructive/mutating requests (document them as
            destructive-skipped). Targets: {{ targets }}. Capture: {{ capture }}. SDK: {{ workspace }}/sdk/.
            Vault: api-security-assessment. Domain: <HOST>. Identity roster: {{ workspace }}/recon/identities.json.

            ANTI-BOT RULES (obey all): mint-once (reuse a captured token; re-mint only on a 401, never
            pre-emptively); use a corpus User-Agent, never curl/python defaults; send the full captured
            header set; throttle to <=2 req/s per endpoint with 50-200ms jitter; use captured
            identifiers, never enumerate id spaces; on a 403/500/"inactive"/"No active account" lockout
            signal, STOP and record a blocker.

            1. Pick your identities from the roster yourself:
                 cat {{ workspace }}/recon/identities.json
               Choose an ATTACKER identity: use its source_indices[0] as <IDENTITY_INDEX> (for
               `burp.py cred`) and its user_agents[0] as the User-Agent <UA>. Choose a VICTIM = a
               DIFFERENT identity and use its user_ids[0] as <VICTIM_ID>. If the roster has only ONE
               identity there is no victim: register a `no-victim-id` blocker (step 5) and mark every
               cross-user/privacy hypothesis blocked.
            2. Compute the slug and read the hypotheses:
                 python3 /mnt/skill_library/api-recon-toolkit/scripts/targets.py show {{ targets }} --index <INDEX> --part both
                 python3 /mnt/skill_library/api-recon-toolkit/scripts/vault_note.py slug "<METHOD> <PATH>"
                 obsidian vault=api-security-assessment read file="<HOST>/endpoints/<slug>.md"
               Take <METHOD> and <PATH> from the targets.py show output.
            3. For EACH hypothesis:
               - Destructive/mutating? Do NOT send. Note it as destructive-skipped, reference
                 [[BLK-destructive-skipped]], and register that blocker.
               - Otherwise test it. Authenticate WITHOUT printing the token — capture it in the SAME
                 command with $(...):
                   TOKEN=$(python3 /mnt/skill_library/api-recon-toolkit/scripts/burp.py cred {{ capture }} --index <IDENTITY_INDEX> --field authorization); \
                   curl -sS -X <METHOD> "https://<HOST><path>" -H "Authorization: $TOKEN" -H "User-Agent: <UA>" <other captured headers> <data>
                 For a PRIVACY/IDOR test, authenticate as the ATTACKER and request the VICTIM's id
                 (<VICTIM_ID>); a 2xx that returns the victim's PII fields = CONFIRMED privacy leak.
                 Record PII by FIELD NAME/presence, never raw values.
            4. Write results to {{ workspace }}/test/<slug>.log.md (write_file):
                 ## Test log — {{ date }}
                 ### H1 — <confirmed | not-vulnerable | inconclusive | blocked>
                 - Attempt: <method path, identity used, UA>
                 - Result: <status + minimal evidence (PII as field names)>
                 - Blocker (if any): [[BLK-<slug>]] — <one line>
               Append it: python3 /mnt/skill_library/api-recon-toolkit/scripts/vault_note.py append --vault api-security-assessment --domain <HOST> --slug <slug> --from {{ workspace }}/test/<slug>.log.md
            4b. For each hypothesis you CONFIRMED (a real, reproduced vulnerability — especially a
                privacy/PII leak), emit a structured finding. Write {{ workspace }}/test/<slug>.<Hn>.finding.json
                (write_file) as:
                  {"title": "<one-line vuln title>",
                   "description": "<what an unauthorized caller obtains and why it is a vuln — PII by field name, never raw values>",
                   "evidence": ["<the EXACT tokenless one-liner that reproduced it: TOKEN=$(python3 /mnt/skill_library/api-recon-toolkit/scripts/burp.py cred {{ capture }} --index <IDENTITY_INDEX> --field authorization); curl -sS -X <METHOD> 'https://<HOST><path>' -H \"Authorization: $TOKEN\" -H 'User-Agent: <UA>' <other headers>>"]}
                NEVER put a live credential in evidence — mint EVERY credential inline with $(...), not
                just the bearer token. If the endpoint authenticates by cookie or another captured header
                instead of (or in addition to) Authorization, mint THAT too and reference the variable:
                  COOKIE=$(python3 /mnt/skill_library/api-recon-toolkit/scripts/burp.py cred {{ capture }} --index <IDENTITY_INDEX> --field cookie); curl ... -H "Cookie: $COOKIE"
                (or --field header:<NAME> for any other auth header). No raw cookie/token/session value
                may appear as a literal in evidence. Then append it:
                  python3 /mnt/skill_library/api-recon-toolkit/scripts/findings.py add --from {{ workspace }}/test/<slug>.<Hn>.finding.json --to {{ workspace }}/findings.jsonl
                (findings.py REJECTS a raw JWT — if it errors, or if any credential is a literal, fix the
                evidence to mint it with $(...).)
            5. For every blocker you hit, register it (idempotent). Write a one-line description to
               {{ workspace }}/test/<slug>.blk.md, then:
                 python3 /mnt/skill_library/api-recon-toolkit/scripts/vault_note.py blocker --vault api-security-assessment --domain <HOST> --id <blocker-slug> --endpoint <slug> --desc-from {{ workspace }}/test/<slug>.blk.md
               Blocker slugs (prefer these): no-second-account, no-victim-id, auth-expired, waf-403,
               rate-limited, mfa-required, destructive-skipped, endpoint-unreachable, needs-write-scope.
            6. Reply ONE line: "<METHOD> <PATH> -> confirmed=<n> findings=<n> blocked=<n>".

          STEP C — report (you do this): after all sub-agents report, write {{ outputs }}/test-report.md —
          confirmed findings by severity (call out privacy/PII leaks), the count of findings emitted to
          {{ workspace }}/findings.jsonl (python3 /mnt/skill_library/api-recon-toolkit/scripts/findings.py
          list {{ workspace }}/findings.jsonl), plus a blocker table: for each BLK-*.md under
          <HOST>/blockers/, its id, status, and the count of affected endpoints. Call present_files on
          {{ outputs }}/test-report.md. The findings.jsonl feeds the next step (Confirm).
```

- [ ] **Step 4: Run the new + affected existing Test-step tests**

Run: `python -m pytest tests/test_api_security_assessment_workflow.py -k "test_test_ or test_step_emits or safe_by_default or findings_evidence" -v`
Expected: PASS — the new self-select test plus the existing `test_test_step_emits_findings_jsonl`, `test_test_prompt_is_safe_by_default_with_antibot_and_blockers`, and `test_test_findings_evidence_mints_any_credential` all pass (every pinned substring is preserved).

- [ ] **Step 5: Commit**

```bash
git add workflows/api-security-assessment.yaml tests/test_api_security_assessment_workflow.py
git commit -m "feat(secassess): Test lead builds identity roster once; sub-agents self-select attacker/victim"
```

---

### Task 5: `capture_recon` — sub-agent derives method/path/host (index-only delegation)

**Files:**
- Modify: `workflows/api-security-assessment.yaml` (`capture_recon` prompt — full rework)
- Test: `tests/test_api_security_assessment_workflow.py`

**Interfaces:**
- Produces: `capture_recon` lead keeps its corpus-wide `hosts`/`index`/`harvest` + per-domain recon write, but its STEP C delegates substituting **only `<INDEX>`**; the sub-agent derives `<METHOD>`/`<PATH>`/`<HOST>` from its own `burp.py view` output.

- [ ] **Step 1: Write failing test**

Add to `tests/test_api_security_assessment_workflow.py`:

```python
def test_capture_recon_is_thin_and_subagent_takes_only_index():
    p = _task(0, 0)  # capture_recon
    assert "COORDINATOR CONTRACT" in p and "in ONE message" in p
    assert "substituting only <INDEX>" in p          # index-only delegation
    assert "<METHOD>/<PATH>/<HOST>" not in p          # old multi-placeholder substitution gone
    # sub-agent still derives method/path from view and files the note create-if-missing
    assert "take <method> and <path>" in p.lower()
    assert "--if-missing" in p and "## Recon — {{ date }}" in p and "--kind recon" in p
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_api_security_assessment_workflow.py::test_capture_recon_is_thin_and_subagent_takes_only_index -v`
Expected: FAIL — `COORDINATOR CONTRACT` / `substituting only <INDEX>` not present; `<METHOD>/<PATH>/<HOST>` still present.

- [ ] **Step 3: Replace the entire `capture_recon` task prompt**

Replace the whole `prompt: |` body of `capture_recon` with (10-space lead indent; 12-space sub-agent block):

```
          You are the LEAD agent coordinating AUTHORIZED reconnaissance of APIs your org owns.
          Your ONLY input this task is the Burp capture at: {{ capture }}
          The toolkit is at /mnt/skill_library/api-recon-toolkit/scripts/ (run with python3 <abs path>).
          The persistent Obsidian vault is "api-security-assessment"; notes are split by DOMAIN at the
          root: <domain>/recon.md and <domain>/endpoints/<slug>.md.

          COORDINATOR CONTRACT (read this first — it governs everything below):
          - Your job is ONLY: run the corpus-wide setup calls, fan out one bash sub-agent per observed
            endpoint, then summarize. You NEVER inspect or document a single endpoint yourself.
          - Fan out ALL endpoints in ONE message: emit EVERY delegate_task call together in a single
            turn, then STOP and wait for their replies. NEVER delegate one endpoint, look at its
            result, then delegate the next — that serial loop is what exhausts your step budget.
          - Every delegate_task MUST set subagent_type="bash" (a general-purpose sub-agent has no
            shell and cannot run the toolkit — it will fail).
          - Do NOT inspect a single endpoint yourself. These are SUB-AGENT-ONLY and you must NEVER run
            them: burp.py view, burp.py cred, burp.py decode-auth, targets.py show, vault_note.py slug,
            obsidian read, or any per-endpoint note write. If you catch yourself about to inspect ONE
            endpoint, STOP and delegate it instead. (burp.py hosts / index / harvest are corpus-wide —
            those you DO run.)
          - After fan-out, do nothing per-endpoint: read only the sub-agents' one-line replies, then
            write the STEP D summary.

          STEP A — map the capture (you do this):
            python3 /mnt/skill_library/api-recon-toolkit/scripts/burp.py hosts {{ capture }}
            python3 /mnt/skill_library/api-recon-toolkit/scripts/burp.py index {{ capture }} --apis-only --format json
          Keep the resulting list of API item indices.

          STEP B — harvest reusable values (you do this):
            python3 /mnt/skill_library/api-recon-toolkit/scripts/burp.py harvest {{ capture }} --format json
          Save the JSON to {{ workspace }}/recon/values.json (write_file). Then, for EACH domain, write a
          recon section to {{ workspace }}/recon/<domain>.recon.md whose FIRST line is the heading
          "## Recon — {{ date }}" followed by: Base URLs, Reusable headers, Cookies, Auth/JWT shapes
          (claims only — NEVER a raw token), Candidate identifiers (real IDs reusable as required fields
          when testing targets), Oracles. APPEND it — this domain may already have recon from a prior
          assessment, and appending preserves it (a fresh run stacks under its own dated heading):
            python3 /mnt/skill_library/api-recon-toolkit/scripts/vault_note.py append \
              --vault api-security-assessment --domain <domain> --kind recon \
              --from {{ workspace }}/recon/<domain>.recon.md

          STEP C — fan out (ONE message, all at once): for EACH item index from STEP A, call
          delegate_task, ALL in a single turn, subagent_type="bash" (REQUIRED). The ONLY value that
          varies between the calls is <INDEX>. Give each sub-agent this EXACT prompt, substituting only
          <INDEX>:

            You are documenting ONE API endpoint for an authorized security assessment. Do only this one.
            Capture: {{ capture }}. Toolkit: /mnt/skill_library/api-recon-toolkit/scripts/.
            Vault: api-security-assessment (notes split by domain at the root).
            1. Inspect ONLY item index <INDEX> (view is truncated/redacted for you):
                 python3 /mnt/skill_library/api-recon-toolkit/scripts/burp.py view {{ capture }} --index <INDEX> --req-headers --resp-headers --cookies
                 python3 /mnt/skill_library/api-recon-toolkit/scripts/burp.py view {{ capture }} --index <INDEX> --req-body --resp-body --keys
                 python3 /mnt/skill_library/api-recon-toolkit/scripts/burp.py view {{ capture }} --index <INDEX> --decode-auth
               From the view output, take <METHOD> and <PATH> (the request line) and <HOST> (the Host
               header / request URL). You derive these yourself — they are NOT given to you.
            2. Compute the note filename stem:
                 python3 /mnt/skill_library/api-recon-toolkit/scripts/vault_note.py slug "<METHOD> <PATH>"
            3. Write the note body to {{ workspace }}/endpoints/<slug>.md using write_file, with EXACTLY this
               frontmatter + sections (fill in the angle brackets):
                 ---
                 endpoint: <METHOD> <PATH>
                 domain: <HOST>
                 auth: <scheme, e.g. Bearer JWT (alg=HS256) | session cookie | query authorizeCode | none>
                 oracle: <yes|no|unknown>
                 source: capture #<INDEX>
                 status: observed
                 tags: [api-recon]
                 ---
                 # <METHOD> <PATH>
                 ## Request shape
                 ## Response shape
                 ## Headers
                 ## Auth
                 ## Oracle
                 <does it confirm the validity/existence of some data? one line + why>
                 ## Observations
            4. File the note into the vault (create-if-missing — a prior assessment may already have
               documented this endpoint; do NOT clobber its hypotheses/tests):
                 python3 /mnt/skill_library/api-recon-toolkit/scripts/vault_note.py put \
                   --vault api-security-assessment --domain <HOST> --slug <slug> \
                   --from {{ workspace }}/endpoints/<slug>.md --if-missing
               If it prints "NOOP: note exists" the endpoint was documented in a prior run — that is
               fine; leave the existing note as-is and do not try to replace it.
            5. Reply with ONE line: "<METHOD> <PATH> -> oracle=<yes|no|unknown> (new|prior-run)".

          STEP D — summarize (you do this): after every sub-agent has reported, write
          {{ outputs }}/recon-summary.md listing each domain, the number of endpoints documented, and the
          most useful reusable identifiers, then call present_files on it. If STEP A found no API items,
          say so plainly and still record whatever cookies/JWTs harvest found.
```

- [ ] **Step 4: Run the new + affected existing tests**

Run: `python -m pytest tests/test_api_security_assessment_workflow.py -k "capture_recon or leads_delegate or shipped_toolkit or rerun_safe" -v`
Expected: PASS — new capture_recon test plus `test_leads_delegate_per_endpoint_to_bash_subagents`, `test_prompts_reference_the_shipped_toolkit_and_vault`, and `test_setup_and_hypothesize_are_rerun_safe` still pass.

- [ ] **Step 5: Commit**

```bash
git add workflows/api-security-assessment.yaml tests/test_api_security_assessment_workflow.py
git commit -m "feat(secassess): capture_recon sub-agent derives method/path/host (index-only delegation)"
```

---

### Task 6: Umbrella invariant + full-suite verification

**Files:**
- Test: `tests/test_api_security_assessment_workflow.py`

**Interfaces:**
- Consumes: all five reworked lead prompts (Tasks 3–5).

- [ ] **Step 1: Add the umbrella test (now all five leads are done)**

Add to `tests/test_api_security_assessment_workflow.py`:

```python
def test_every_lead_has_contract_and_single_batch():
    for t in _all_tasks():
        p = t.prompt
        assert "COORDINATOR CONTRACT" in p
        assert "in ONE message" in p
        assert "Do NOT" in p
        assert 'subagent_type="bash"' in p
        assert t.recursion_limit == 600
```

- [ ] **Step 2: Run the full workflow test file**

Run: `python -m pytest tests/test_api_security_assessment_workflow.py -v`
Expected: PASS (all tests, old and new).

- [ ] **Step 3: Run the whole suite to catch regressions**

Run: `python -m pytest -q`
Expected: PASS — the full suite is green (the memory notes ~571 tests green on the prior branch; expect that plus the new cases, none failing).

- [ ] **Step 4: Sanity-check the YAML still parses as a workflow**

Run: `python -c "import yaml; from atom.workflow.schema import WorkflowDef; wf = WorkflowDef.model_validate(yaml.safe_load(open('workflows/api-security-assessment.yaml'))); print([t.id for s in wf.steps for t in s.tasks], [t.recursion_limit for s in wf.steps for t in s.tasks])"`
Expected: prints `['capture_recon', 'build_sdk', 'hypothesize', 'test', 'confirm'] [600, 600, 600, 600, 600]`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_api_security_assessment_workflow.py
git commit -m "test(secassess): umbrella coordinator-contract invariant across all lead prompts"
```

---

## Self-Review

**Spec coverage** (against `2026-07-21-api-security-coordinator-fanout-design.md`):
- §1 Coordinator Contract on all 5 leads → Tasks 3 (build_sdk/hypothesize/confirm), 4 (test), 5 (capture_recon), umbrella in 6. ✓
- §2 Test redesign (roster→file, drop per-endpoint placeholders, self-select, inline anti-bot) → Task 4. ✓
- §3 Other coordinators (hypothesize/build_sdk/confirm contract + capture_recon index-only) → Tasks 3 & 5. ✓
- §4 Per-task recursion_limit backstop (schema/runtime/engine + YAML 600) → Tasks 1 & 2. ✓
- §5 Tests (workflow prompt asserts + schema/runtime) → Tasks 1–6. ✓

**Placeholder scan:** No TBD/TODO; every code and prompt block is complete literal text. The `<INDEX>`, `<HOST>`, `<METHOD>`, `<IDENTITY_INDEX>`, `<VICTIM_ID>`, `<UA>` tokens are intentional prompt-template fill-ins (the sub-agent computes the latter three from the roster/view), not plan placeholders.

**Type consistency:** `TaskDef.recursion_limit: Optional[int]` (Task 1) is read as `td.recursion_limit` (Task 1 engine edit) and asserted as `t.recursion_limit == 600` (Tasks 2, 6). `run_agent(override_recursion_limit=...)` name matches between the signature edit, the engine call, and the resolution line. `build_run_config` spy signature matches its real positional call `(thread_id, limit, trace, obs_provider)`. Test helpers `_load()`, `_all_tasks()`, `_task(step_idx, task_idx)` are defined before first use.

**Note on a spec refinement:** the spec's §5 said "assert the old `<IDENTITY_INDEX>`/`<VICTIM_ID>` placeholders are absent." Those tokens remain in the Test **sub-agent** block as sub-agent-computed fill-ins, so the test instead asserts the real invariant — the **lead** no longer maps identities (`"You will pass the relevant identities" not in p`) and the sub-agent self-selects (`"Pick your identities from the roster"`, `source_indices`, `user_ids`). Same guarantee, correct assertion.
