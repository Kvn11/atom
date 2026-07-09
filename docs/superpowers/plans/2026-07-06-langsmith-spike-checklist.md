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

## Finding (minor, tag hygiene) — filter on metadata, not `role:*` tags

The sub-agent run carries **both `role:subagent` and a leaked `role:lead` tag**. Cause: LangChain
*unions* run **tags** from parent → child down the trace tree, so the lead's `role:lead` rides onto the
nested sub-agent run even though `build_subagent_trace` strips `role:lead` from the trace it sets. This
is a LangChain tracer behavior, not controllable via the run-config dict (tags are additive/inheritable;
there is no clean per-child tag removal through `config`).

Impact: **none on threading or metadata.** Run **metadata** is key-overridden (not unioned), so
`agent_role`/`is_subagent`/`session_id`/`subagent_type` are clean and correct on both runs. Only the
`role:lead` *tag* is polluted onto sub-agents.

**Guidance:** to distinguish leads from sub-agents, filter on **metadata** — `is_subagent` (bool) or
`agent_role` — which is authoritative. `role:subagent` / `subagent_type:*` tags are also unambiguous for
*finding* sub-agents; only "leads-only via the `role:lead` tag" is unreliable (it also matches
sub-agents) — use `is_subagent = false` instead.

Open decision (not yet actioned): whether to keep the `role:*` tags as-is (metadata is the source of
truth) or drop them in favor of metadata-only role filtering to avoid the leak. Left to Kevin.

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
