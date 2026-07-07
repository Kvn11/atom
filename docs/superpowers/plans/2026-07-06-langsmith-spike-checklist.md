# LangSmith thread-grouping spike checklist

**STATUS: PENDING MANUAL EXECUTION — requires LANGSMITH_API_KEY (owner: Kevin)**

This is a template for the manual verification spike described in Task 5 (Step 5) of the
LangSmith observability plan. It has **not** been executed yet — no `LANGSMITH_API_KEY` was
available in the environment that implemented Tasks 1-5. Nothing below should be read as a
verified result until the checkboxes are filled in by whoever runs the spike.

## Checklist

- [ ] In a scratch checkout, export `LANGSMITH_API_KEY`, `LANGSMITH_TRACING=true`,
      `LANGSMITH_PROJECT=atom-spike`.
- [ ] Run a small workflow whose lead agent delegates at least one sub-agent (e.g. adapt
      `workflows/parallel-poems.yaml` to add a `delegate_task` step, or run any workflow whose
      prompt instructs a delegation).
- [ ] In the LangSmith UI, open the project's **Threads** view and confirm:
  - [ ] Each task appears as a distinct thread named by its `session_id`
        (`<run>:s<step>:<task>`).
  - [ ] The lead run and its sub-agent run(s) appear in the **same** thread.
  - [ ] Sub-agent runs carry `is_subagent=true`, `role:subagent`, and a `system_prompt_sha`
        that differs from the lead's.
  - [ ] LangGraph's auto `metadata.thread_id` (the child id, for sub-agents) does **not** split
        sub-agents into their own threads.
- [ ] If sub-agents split into separate threads, apply the documented fallback (below) and
      re-run. Record which key LangSmith honored.

## Fallback if sub-agents split into separate threads

If Step 3's last checkbox fails — i.e. LangGraph's own `metadata.thread_id` wins over our
`session_id` and sub-agents end up in their own thread instead of their parent lead's — the
documented fix is to also set `metadata["thread_id"] = session_id` in **both** `build_lead_trace`
and `build_subagent_trace` (`src/atom/observability.py`), then re-run the spike. If that fallback
is applied, add a regression test to `tests/test_observability.py` asserting
`metadata["thread_id"] == session_id` on both builders, and commit the code + test alongside the
updated version of this checklist.
