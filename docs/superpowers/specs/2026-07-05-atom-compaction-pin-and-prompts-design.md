# atom — Compaction Instruction-Pin + Production Prompt Templates

**Status:** Approved (2026-07-05)
**Scope:** One implementation plan. Backend changes are strict TDD; prompt templates are content edits verified by render tests.

## 1. Motivation

atom compacts a thread's history with LangChain's `SummarizationMiddleware` once input reaches
`ratio` (0.5) of the model's context window: everything before a safe cutoff is summarized into a
single `HumanMessage`, the last `keep_messages` (20) are kept verbatim. This is wired in
`src/atom/agent.py::_build_middlewares` via `src/atom/middleware/compaction.py::build_compaction_middleware`,
with atom's own `src/atom/prompts/summary.md` as the summary prompt.

Two facts drive this spec:

1. **The developer system prompt is already safe.** `create_agent(system_prompt=…)` injects the
   system prompt at model-call time; it never enters `state["messages"]`, and compaction only
   rewrites `state["messages"]`. `lead_system.md` behavior survives compaction untouched. No change
   needed there.

2. **The user's original instruction is NOT structurally preserved.** The thread's first
   `HumanMessage` (the task/goal — or, for a workflow task, the rendered task prompt) sits in the
   *summarize* partition on every compaction. It survives only if the summarizer LLM chooses to
   extract it, and two mechanisms actively work against that:
   - `SummarizationMiddleware` trims the to-be-summarized block to `trim_tokens_to_summarize=4000`
     with `strategy="last"` (keeps the *most recent* tokens). On a long thread the original
     instruction is the oldest content and can be **trimmed off before the summarizer ever reads
     it** — guaranteed loss regardless of prompt wording.
   - On the second compaction the input is `[prior summary] + [recent tail]`, so any drift in the
     first summary **compounds** each cycle.

   "Always captured" therefore requires a **structural pin**, not prompt wording alone.

## 2. Locked decisions

| # | Decision |
|---|---|
| D1 | Guarantee = **structural pin + strengthened summary prompt** (not prompt-only). |
| D2 | What is pinned = **the thread's first `HumanMessage`** (verbatim). Deterministic; uniform across chat threads, workflow tasks, and subagents (each starts with its instruction as message 0). Later user-stated constraints remain captured in the summary body. |
| D3 | The pin lives in a **dedicated `ThreadState` channel**, captured once, re-injected verbatim on every compaction. It is never summarized and cannot drift. |
| D4 | Rewrite the harness-owned templates to production grade: `summary.md`, `lead_system.md`, `subagent_general.md`, `subagent_bash.md`. `user_task.md` stays a passthrough. |
| D5 | Expose `compaction.summary_input_tokens` (default **8000**) → `SummarizationMiddleware(trim_tokens_to_summarize=…)`, so production summaries see more history. |

## 3. Component A — the instruction pin

### 3.1 State channel

`src/atom/state.py` — add one channel to `ThreadState`:

```python
# The thread's first user instruction, captured once by InstructionPinMiddleware and
# re-injected verbatim into every compaction so it can never be trimmed or paraphrased away.
# Write-once (the capture middleware only writes when absent); default channel semantics
# (LastValue) are fine because there is exactly one write.
pinned_instruction: NotRequired[str]
```

No reducer (write-once). It lives outside `messages`, so `RemoveMessage(REMOVE_ALL_MESSAGES)` during
compaction never clears it, and it is checkpointed, so it persists across turns.

### 3.2 Capture middleware

`src/atom/middleware/instruction_pin.py` (new) — `InstructionPinMiddleware(AgentMiddleware)`,
single responsibility, `before_agent` hook. Placed in the `before_agent` group of the chain
(after `ThreadDataMiddleware`/`SandboxMiddleware`/`UploadsMiddleware` — ordering among `before_agent`
hooks is immaterial to correctness since compaction runs later, in `before_model`).

```python
from atom.messages import message_text

class InstructionPinMiddleware(AgentMiddleware):
    def before_agent(self, state, runtime):
        if state.get("pinned_instruction"):        # idempotent: turn 2+ already has it
            return None
        for msg in state.get("messages", []):
            if isinstance(msg, HumanMessage):
                text = message_text(msg).strip()
                if text:
                    return {"pinned_instruction": text}
                break                               # first human msg empty → nothing to pin
        return None
```

- Uses `message_text` (handles list-content reasoning models).
- Idempotent: only writes when the channel is empty; on a resumed thread it is already set.
- If there is no non-empty first `HumanMessage`, it writes nothing and the pin degrades to a no-op
  (compaction behaves exactly as the library default).

### 3.3 Re-injection: `PinnedSummarizationMiddleware`

`src/atom/middleware/compaction.py` — add a subclass of `SummarizationMiddleware` and have
`build_compaction_middleware` instantiate it. It overrides **both** `before_model` and
`abefore_model` (async is the workflow hot path; both must be implemented):

```python
from langchain_core.messages import HumanMessage, RemoveMessage

_PIN_PREFIX = "[Standing instruction — the user's original request, preserved verbatim]\n\n"

class PinnedSummarizationMiddleware(SummarizationMiddleware):
    def before_model(self, state, runtime):
        result = super().before_model(state, runtime)
        return self._inject_pin(result, state)

    async def abefore_model(self, state, runtime):
        result = await super().abefore_model(state, runtime)
        return self._inject_pin(result, state)

    def _inject_pin(self, result, state):
        # result is None when no compaction happened → pass through unchanged.
        if not result:
            return result
        pinned = (state.get("pinned_instruction") or "").strip()
        if not pinned:
            return result
        msgs = result["messages"]
        # super() returns [RemoveMessage(ALL), <summary HumanMessage>, *preserved].
        # Splice the pin in immediately AFTER the RemoveMessage, BEFORE the summary.
        insert_at = 1 if (msgs and isinstance(msgs[0], RemoveMessage)) else 0
        pin_msg = HumanMessage(
            content=f"{_PIN_PREFIX}{pinned}",
            additional_kwargs={"lc_source": "pinned_instruction"},
        )
        result["messages"] = [*msgs[:insert_at], pin_msg, *msgs[insert_at:]]
        return result
```

Resulting message list after compaction:
`[RemoveMessage(ALL), <pinned instruction verbatim>, <summary>, …last 20 verbatim…]`.

Because the pin is rebuilt from durable state every cycle, it cannot be trimmed away (the
`strategy="last"` hole) or drift across repeated compactions. The **old** injected pin from the
previous compaction lands in the next summarize-partition and is cleared by `RemoveMessage(ALL)`,
then replaced by a fresh verbatim pin — no accumulation.

`build_compaction_middleware` gains a `trim_tokens: int | None = None` parameter, passed straight
through as `SummarizationMiddleware(trim_tokens_to_summarize=…)` when set.

### 3.4 Wiring

`src/atom/agent.py::_build_middlewares`:
- Add `InstructionPinMiddleware()` to the `before_agent` group.
- Pass `trim_tokens=cfg.compaction.summary_input_tokens` into `build_compaction_middleware`.

`src/atom/subagent.py` (subagent runner, which builds its own compaction middleware when a
summarizer is set): pass the same `trim_tokens` through, and add `InstructionPinMiddleware()` to the
subagent middleware list so a long-running delegated task also pins its delegated prompt.

## 4. Component B — `summary.md` rewrite

Rewrite `src/atom/prompts/summary.md` to:
- State up front that the user's original instruction is **pinned separately and always present**,
  so the summary must **not** re-summarize or restate it — spend the budget on progress instead.
- Keep a tight, checklist-style extraction target: current plan / todo state (done / in-progress /
  pending), every workspace / uploads / outputs virtual path and each file created or modified,
  deliverables already presented via `present_files`, key results / values / findings that avoid
  redoing work, and open questions still unanswered.
- Explicitly drop verbose tool output, superseded reasoning, and anything already replaced.
- Preserve the required `{messages}` placeholder and its wrapping (SummarizationMiddleware contract).

## 5. Component C — production prompt tightening

Rewrite for clarity, stronger planning/deliverable discipline, and output quality, preserving all
Jinja variables currently referenced (StrictUndefined will raise on any dropped/renamed variable):
- `src/atom/prompts/lead_system.md`
- `src/atom/prompts/subagent_general.md`
- `src/atom/prompts/subagent_bash.md`

`src/atom/prompts/user_task.md` is unchanged (documented passthrough).

## 6. Config

`src/atom/config/schema.py::CompactionConfig` — add:

```python
summary_input_tokens: int = 8000   # trim_tokens_to_summarize: how much history the summarizer reads
```

`config.yaml` `compaction:` block — add the field with an explanatory comment.

## 7. Testing (backend, strict TDD)

New/extended tests under `tests/`:
- **Capture:** first `HumanMessage` (str content) → `pinned_instruction` set to its text; list-content
  (thinking + text blocks) → text-only extraction; no human message / empty first human message →
  channel left unset.
- **Idempotence:** a second `before_agent` call with the channel already set does not overwrite it.
- **Injection — single compaction:** after a triggered compaction, the pinned text appears **verbatim**
  as a `HumanMessage` positioned **after** `RemoveMessage` and **before** the summary message.
- **Injection — drift-proof across two compactions:** drive two successive compactions; the pinned
  text is still byte-for-byte identical after the second (the core guarantee).
- **No-compaction passthrough:** below the trigger, `before_model` returns `None` and messages are
  untouched (no pin injected).
- **Empty pin passthrough:** compaction with an unset `pinned_instruction` yields the exact library
  result (no injected pin, no crash).
- **Async parity:** the `abefore_model` path injects the pin identically to `before_model`.
- **Config:** `summary_input_tokens` default is 8000 and flows into the middleware.
- **Templates:** extend existing prompt-render tests — each rewritten template renders under
  `StrictUndefined` with a representative context, and asserts a couple of stable anchor phrases /
  required sections are present. `summary.md` still contains `{messages}`.

Run with `.venv/bin/python -m pytest` (NOT `.venv/bin/pytest`).

## 8. Non-goals (separate specs / later)

- The **first production-grade workflow** content (next spec).
- The **evaluation harness** for prompt quality (next spec).
- Changing the compaction `ratio` or `keep_messages` defaults.
- A configurable operator-level "standing directive" distinct from the task (considered and
  deferred — the developer `system_prompt` already covers always-in-force rules).
- Per-subagent differences in what is pinned (subagents pin their delegated prompt via the same
  mechanism).
