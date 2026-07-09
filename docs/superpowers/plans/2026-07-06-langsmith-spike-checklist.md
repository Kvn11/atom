# LangSmith thread-grouping spike — EXECUTED

**STATUS: DONE — verified live on 2026-07-09 against LangSmith project `atom`. RESULT: PASS.**

The manual verification spike from Task 5 (Step 5) of the LangSmith observability plan has been run
end-to-end with a real `LANGSMITH_API_KEY` and a real Anthropic (`haiku`) workflow that delegated a
sub-agent. Traces were queried back via the LangSmith SDK (`list_runs` + `list_threads`).

## Result

- [x] Workflow ran with `LANGSMITH_TRACING=true`, `LANGSMITH_PROJECT=atom`; lead delegated one
      `general-purpose` sub-agent.
- [x] **Lead run** `obs_smoketest_<sfx>/Probe/probe`: `session_id=<run>:s0:probe`, `agent_role=lead`,
      `is_subagent=False`, `system_prompt_ref=@prompts/lead_system.md` + `system_prompt_sha`,
      `model=haiku`, tags incl. `role:lead`/`workflow:*`/`profile:default`/`model:haiku`/`atom-workflow`.
- [x] **Sub-agent run** `.../probe/sub:echo probe`: `is_subagent=True`, `agent_role=subagent`,
      `subagent_type=general-purpose`, `system_prompt_ref=@prompts/subagent_general.md` with a sha that
      **differs** from the lead's.
- [x] **Same thread:** the sub-agent's `session_id` equals the lead's `session_id`.
- [x] **`list_threads(project_name="atom")` returns a thread keyed by that `session_id`** — so
      **LangSmith honors `session_id` as the thread key. The `thread_id` fallback below was NOT needed.**
- [x] LangGraph's auto `metadata.thread_id` (unique child id) did **not** split the sub-agent into its
      own thread; the sub-agent nests within the lead's single trace/thread.

## Finding (tag hygiene) — RESOLVED 2026-07-09

**Original observation:** the sub-agent run carried **both `role:subagent` and a leaked `role:lead`
tag**. Cause: LangChain *unions* run **tags** parent → child down the trace tree, so a lead's `role:lead`
tag rides onto its nested sub-agent runs. Run **metadata** is key-overridden (not unioned), so
`agent_role`/`is_subagent`/`session_id`/`subagent_type` were always clean; only the `role:lead` *tag*
leaked.

**Fix applied (commit on `main`):** leads now carry **no role tag at all** — role is metadata-only
(`agent_role` / `is_subagent`) for leads, which cannot leak since metadata is per-key overridden.
Sub-agents keep a clean `role:subagent` (+ `subagent_type:*`) tag, which only flows *downward* onto their
own children and never up onto leads. Implemented by removing `"role:lead"` from `build_lead_trace`
(`src/atom/observability.py`); `build_subagent_trace` retains its defensive `role:lead` strip.

**Re-verified live (project `atom`):** lead tags = `[model:*, profile:*, atom-workflow, workflow:*,
step:*, task:*, run:*]` (no `role:lead`); sub-agent tags = `[…, role:subagent, subagent_type:*]` (no
`role:lead`). Thread grouping via `session_id` still intact. `scratchpad/obs_smoketest.py` asserts the
leak is gone.

**Filtering guidance:** find sub-agent work via the `role:subagent` tag (or `is_subagent`/`agent_role`
metadata); find lead runs via `is_subagent = false` / `agent_role = lead` metadata (leads intentionally
have no role tag).

## Reproduce

Harness: `scratchpad/obs_smoketest.py` (loads `.env`, sets `LANGSMITH_TRACING=true` +
`LANGSMITH_PROJECT=atom`, runs a one-task workflow that delegates a sub-agent, flushes tracers via
`wait_for_all_tracers()`, then asserts the above via `client.list_runs` / `client.list_threads`).
Notes learned: LangSmith `list_runs` caps `limit` at 100; trace ingestion is async so poll a few
seconds after `wait_for_all_tracers()`.

## Fallback (NOT applied — grouping worked via session_id)

If a future LangSmith/LangGraph change ever made the auto `thread_id` win over `session_id` and split
sub-agents into their own thread, the documented fix is to also set `metadata["thread_id"] = session_id`
in **both** `build_lead_trace` and `build_subagent_trace` (`src/atom/observability.py`), add a regression
test asserting `metadata["thread_id"] == session_id` on both builders, and re-run this spike.
