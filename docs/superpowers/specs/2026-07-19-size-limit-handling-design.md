# Size-limit handling: model input overflow + LangFuse trace-size overflow

- **Date:** 2026-07-19
- **Status:** Design — approved decisions recorded; pending written-spec review
- **Related:** [error-handling-hardening](2026-07-10-error-handling-hardening-design.md),
  [atom-compaction-pin-and-prompts](2026-07-05-atom-compaction-pin-and-prompts-design.md),
  [langfuse-observability-integration](2026-07-16-langfuse-observability-integration-design.md),
  [langsmith-run-exporter](2026-07-09-langsmith-run-exporter-design.md)

## 1. Context & problem

Two live failures, both "payload too big," at two different boundaries:

1. **Model input overflow.** A workflow task died with
   `provider unavailable after 1 attempt(s): ClientError: 400 INVALID_ARGUMENT … The input token
   count exceeds the maximum number of tokens allowed`.
   Path: the Gemini call returns a `400`, `LLMErrorHandlingMiddleware` classifies it non-retryable
   (correctly — `is_retryable` matches no marker for a 400), raises `ProviderUnavailableError` after
   one attempt, and the task fails at `workflow/engine.py:503` with a **misleading** "provider
   unavailable" error (the provider was fine; *we* sent too many tokens).

2. **LangFuse trace-size overflow.** Running the **UI export** on a halted run failed with
   `status code 422: observations in trace are too large: 80.30mb exceeds limit of 80.00mb`.
   LangFuse *accepted* the oversized trace at ingestion but its read API (`client.api.trace.get`)
   refuses to **return** a trace whose observations aggregate past 80 MB. So this surfaces on the
   **download/export** path (`observability/langfuse_export.py`), which currently lets the error
   propagate and kill the whole export.

**Shared root cause.** Oversized content — a giant tool result or a long accumulated message
history — is the fuel for both. It overflows the model's input window *and* bloats the telemetry
trace. So the fix has one shared preventive lever plus one independent safety net at each boundary.

### Why existing proactive compaction did not prevent #1

`build_compaction_middleware` (LangChain `SummarizationMiddleware`, trigger = 50 % of the resolved
context window) runs in `before_model`, but it keeps the last N messages **verbatim**. It therefore
structurally cannot rescue the likely triggers: a single tool result larger than the window, a
profile/window mismatch for Gemini (`max_input_tokens` wrong), a token-count undercount vs. the
server, or the summarizer itself failing/overflowing and keeping full history.

## 2. Goals / non-goals

**Goals**
- Recover from model input overflow by shrinking the request and retrying; if it still can't fit,
  fail with an **accurate** error instead of "provider unavailable."
- Prevent a single tool result from ever entering history at unbounded size, and make the LLM
  **aware** it was truncated so it can adapt (re-run narrower / paginate / grep).
- Make LangFuse export **succeed with full data** on oversized traces (paginate instead of the one
  giant `trace.get`), and cap telemetry payloads at the source so future traces stay readable.
- Provider-agnostic overflow detection (Gemini/Anthropic/OpenAI/Qwen).
- Apply model-side handling to **both** the lead agent and sub-agents.

**Non-goals**
- Perfect/accurate token counting. The provider is the source of truth for "too big"; recovery is
  shrink → retry → let the provider re-judge. This sidesteps the whole token-count-mismatch bug
  class (which likely caused #1).
- A trace-level aggregate byte budget for telemetry (a per-field/per-observation cap fixes the
  reported case and any realistic run; a pathological run with hundreds of near-cap observations is
  a deferred follow-up — §9).
- Retro-shrinking traces already stored oversized (export-side pagination reads them as-is).

## 3. Approved decisions

| # | Decision | Choice |
|---|----------|--------|
| a | Model recovery mechanism | **Deterministic hard-trim** (no extra LLM call; guaranteed to shrink; handles the giant-single-message case). Summarization stays the graceful *proactive* layer. |
| b | Shared preventive cap | **Yes — cap tool output at creation**, with an **LLM-visible** truncation marker that instructs the model how to recover the omitted portion. |
| c | Where LangFuse 422 appears | **UI export (read/download) path.** Export-side pagination is the load-bearing fix; the write-side `mask` is preventive-for-future. |
| D | Doc/plan structure | **One design doc, two implementation plans** (model-side; telemetry-side). Layers are independent and independently mergeable. |

## 4. Architecture — three layers

```
                       ┌─────────────────────── a run accumulates content ───────────────────────┐
   tool executes ─▶ [Layer 1: ToolOutputCap] ─▶ ToolMessage (capped, LLM-visible marker) ─▶ state
                                                                                              │
   state ─▶ before_model (proactive summarize) ─▶ wrap_model_call:                           │
                              [LLMErrorHandling(retry)] ▶ … ▶ [Layer 2: ContextOverflow] ▶ model
                                                                                              │
   state ─▶ LangChain callbacks ─▶ [Layer 3a: truncating mask] ─▶ LangFuse ingest            │
   export ─▶ [Layer 3b: resilient paginated fetch] ◀────────────── LangFuse read  ───────────┘
```

| Layer | Boundary | Kind | Fixes |
|-------|----------|------|-------|
| 1 — tool-output cap | tool result → state | preventive (shared) | #1 + #2 at the source |
| 2 — context-overflow middleware | request → model | safety net | #1 |
| 3a — truncating mask | telemetry → LangFuse write | preventive | #2 (future) |
| 3b — resilient export | LangFuse read → disk | safety net | #2 (existing oversized traces) |

A shared `truncate_text` helper (§8) backs Layers 1, 2 (single-message truncation), and 3a.

---

## 5. Model-side components (Plan 1)

### 5.1 `is_context_overflow(exc) -> bool` — `middleware/llm_error.py`
Pure function beside `is_retryable`. Returns `True` for permanent-for-this-input context overflow
across providers; **must be disjoint from `is_retryable`** (overflow is never a transient retry).
Recognizers (exact strings pinned via TDD fixtures):
- Google GenAI: `code == 400` **and** text contains `input token count` / `token count exceeds` /
  `exceeds the maximum number of tokens`.
- Anthropic: `400` with `prompt is too long` / `maximum … tokens`.
- OpenAI: `context_length_exceeded` / `maximum context length` / `reduce the length`.
- Generic markers: `context window`, `context length`, `too many tokens`, `input is too long`.
Negatives that must return `False`: `429`, any `5xx`, and a `400` that is *not* about size.

### 5.2 `trim_messages_to_budget(messages, approx_budget, *, keep_system=True) -> list` — new `middleware/context_overflow.py`
Pure, deterministic. Approximate token estimate = `chars / 4` (accuracy irrelevant — the retry loop
is the real safety net). Rules:
1. Always keep the system message and the pinned-instruction message (identified via
   `additional_kwargs["lc_source"] == "pinned_instruction"`, matching `compaction.py`).
2. Drop **oldest complete turns** first; never split an `AIMessage.tool_calls` from its matching
   `ToolMessage` (drop the pair together). Keeps the most-recent turns.
3. If, after dropping, a **single** retained message still exceeds `approx_budget`, truncate its
   content in place via `truncate_text` with an elision marker (handles the giant-single-message
   case that summarization can't).
State is **not** mutated — this trims only the per-call `request.messages`; durable reduction is
left to the next turn's `before_model` compaction + `DanglingToolCallMiddleware` repair.

### 5.3 `ContextOverflowMiddleware` — `middleware/context_overflow.py`
Implements `wrap_model_call` / `awrap_model_call` **only**. Positioned as the **innermost**
wrap_model_call middleware (see §7) so it trims exactly what the model receives (after skill/image
injection). Behavior on a model call:
```
try: return handler(request)
except Exception as exc:
    if not is_context_overflow(exc): raise          # transient/other → let LLMErrorHandling handle
    for attempt in range(max_attempts):             # default 3
        budget = int(limit * target_ratio / (2 ** attempt))   # 0.5, 0.25, 0.125 × limit
        request.messages = trim_messages_to_budget(request.messages, budget)
        try: return handler(request)
        except Exception as e2:
            if not is_context_overflow(e2): raise
    raise ContextOverflowError(limit=limit, attempts=max_attempts, original=exc)
```
`limit` comes from `resolve_context_window(model, spec)` (already used by compaction). Config:
`overflow_recovery` (feature toggle; when off, first overflow → `ContextOverflowError` immediately),
`overflow_max_attempts` (3), `overflow_target_ratio` (0.5 — leaves headroom for output tokens).

### 5.4 `ContextOverflowError` + `LLMErrorHandling` tweak — `middleware/llm_error.py`
New exception with a precise message:
`context window exceeded: input still over the model's limit (~M tokens) after K emergency-compaction
attempts; reduce input, raise compaction aggressiveness, or use a larger-window model`.
`run_with_retry_{sync,async}` must let it **propagate unwrapped** — add, as the first line of the
`except`, `if isinstance(exc, ContextOverflowError): raise`. So it survives to `ts.error` verbatim
instead of being re-wrapped into `ProviderUnavailableError`. **No engine change** — the generic
`except Exception` at `engine.py:503` renders `ContextOverflowError: …` faithfully.

### 5.5 `ToolOutputCapMiddleware` — new `middleware/tool_output_cap.py`
Implements `wrap_tool_call` / `awrap_tool_call`, positioned as the **outermost** wrap_tool_call
middleware (first among tool wrappers) so the capped `ToolMessage` is what enters state. Behavior:
call `handler(request)`, then if the result carries content over `max_tool_output_chars`, rewrite it
keeping a **head + tail** slice with the elided middle replaced by an **LLM-actionable** marker:

> `\n\n[atom: tool output truncated to fit context — showing the first {head} and last {tail} of
> {total} characters ({elided} elided). To see the omitted portion, re-run this tool with a narrower
> scope: grep/filter, a smaller range or page, or head/tail.]\n\n`

Handles `ToolMessage.content` as `str` **or** `list[content-block]` (cap the text blocks); if the
handler returns a `Command`, cap the `ToolMessage`s inside it. Applies to every tool including
`delegate_task` (sub-agent results can be large) and bash/file reads. Config: `tools.max_output_chars`
(default `100_000` ≈ ~25 k tokens — generous but bounded). Because the trigger is a high threshold,
existing tests with small outputs are unaffected.

---

## 6. Telemetry-side components (Plan 2)

### 6.1 Truncating `mask` — `observability/provider.py`
The v3 SDK applies `mask(*, data, **kwargs) -> Any` to every observation's input/output/metadata
before export, and its serializer explicitly does **not** length-truncate strings. Add
`_truncating_mask` and pass it: `Langfuse(…, mask=_truncating_mask)` in `_default_langfuse_factory`
(line ~114). The `CallbackHandler` binds to that global client, so all auto-captured spans are
covered. Behavior: recursively truncate string leaves longer than `max_field_chars` (default
`100_000`) via `truncate_text`; as an outer guard, if a single observation's serialized size still
exceeds `max_observation_bytes` (default `2_000_000`) replace the payload with a marker. The mask
must **never raise** (telemetry must not break a run) — wrap in try/except returning `data` on error.
Config under `observability.langfuse`.

### 6.2 Resilient export — `observability/langfuse_export.py`
Replace the direct `client.api.trace.get(_item_id(it))` in `fetch_session_traces` with
`_fetch_trace_resilient(client, trace_id)`:
1. Try `client.api.trace.get(id)` (fast path, unchanged for normal traces).
2. On a too-large `422` (or any fetch error), fall back to a **data-preserving** assembly:
   - `client.api.trace.get(id, fields="core")` → trace-level fields incl. `metadata` (required by
     `_is_lead` / `_for_task`; `update_trace=True` already promotes atom's keys to trace level).
   - `client.api.observations.get_many(trace_id=id, cursor=…, limit=1000)` paginated (cursor-based)
     to collect all observations, each page bounded well under 80 MB; attach as the trace's
     `observations`.
   - Truncate any single observation still over a per-item cap via `truncate_text`.
   Return the same JSON-safe dict shape `_as_dict` produces so `build_envelope` is unchanged.
3. If even the fallback fails, **skip with a placeholder** dict (retains id + metadata if known),
   log a warning, and mark the export incomplete.

Completeness: a fully-skipped trace flips `ExportResult.complete = False`; a truncated-but-present
trace stays complete (lead coverage intact) but is flagged in a log line. Keep `ExportResult`
changes minimal (reuse `complete`; add an optional count of degraded traces only if trivial).

---

## 7. Middleware placement (load-bearing)

**Lead agent (`agent.py:_build_middlewares`):**
- `ToolOutputCapMiddleware()` inserted as the **first** wrap_tool_call middleware — immediately
  before `SandboxAuditMiddleware` (line ~312).
- `ContextOverflowMiddleware(...)` inserted as the **innermost** wrap_model_call middleware — after
  the `DeferredToolFilterMiddleware` block (line ~305), before `TodoListMiddleware`. It stays inside
  `LLMErrorHandlingMiddleware` (position 5, outermost), so overflow raised innermost propagates up to
  the retry layer, which now passes `ContextOverflowError` through unwrapped.

**Sub-agents (`subagent.py:_child_middleware`):** append `ContextOverflowMiddleware` **after**
`LLMErrorHandlingMiddleware` (keeps retry outer), and add `ToolOutputCapMiddleware` as the outermost
tool wrapper. This finally makes the docstring's "survive … context overflow" claim (line 99) true.

Separation of concerns holds: `ContextOverflowMiddleware` handles *only* overflow (re-raises
anything else); `LLMErrorHandlingMiddleware` handles *only* transient retry; a transient error during
a trimmed retry propagates out and is retried normally with the original request.

## 8. Shared helper — `truncate_text`

One util (new `atom/limits.py`) reused by Layers 1, 2, 3a:
`truncate_text(text: str, *, max_chars: int, marker: str, keep: "middle"|"tail" = "middle") -> str`.
- `keep="middle"` → head+tail retained, marker in the elided middle (tool outputs, single messages).
- `keep="tail"` isn't needed initially; default middle. Callers pass a context-specific `marker`
  (Layer 1 = LLM-actionable instruction; Layer 2/3a = neutral "elided by size cap").

## 9. Config summary

| Key | Default | Layer |
|-----|---------|-------|
| `tools.max_output_chars` | `100_000` | 1 |
| `compaction.overflow_recovery` | `true` | 2 |
| `compaction.overflow_max_attempts` | `3` | 2 |
| `compaction.overflow_target_ratio` | `0.5` | 2 |
| `observability.langfuse.max_field_chars` | `100_000` | 3a |
| `observability.langfuse.max_observation_bytes` | `2_000_000` | 3a |

## 10. Testing strategy

- **`is_context_overflow`** — table test with real per-provider overflow strings + negatives
  (`429`, `5xx`, non-size `400`); assert disjoint from `is_retryable`.
- **`trim_messages_to_budget`** — keeps system + pin; drops oldest turns; never orphans a tool-call
  pair; truncates a single oversized message; always terminates under budget.
- **`ContextOverflowMiddleware`** — fake model that overflows once then succeeds → recovers;
  overflows always → `ContextOverflowError` after K attempts; a transient error mid-retry still
  propagates to (and is retried by) `LLMErrorHandling`; `ContextOverflowError` reaches `ts.error`
  unwrapped (extend `test_workflow_engine.py`; keep the existing "provider unavailable" transient
  assertion at line 494 green).
- **`ToolOutputCapMiddleware`** — over-threshold `str` and `list` content truncated with the marker;
  under-threshold untouched; `Command`-wrapped ToolMessages capped; marker text present so the model
  can act.
- **`_truncating_mask`** — big string leaf truncated; nested dict/list walked; oversized observation
  guarded; never raises on odd input.
- **Resilient export** — fake client whose `trace.get(id)` raises a 422-too-large but whose
  `trace.get(id, fields="core")` + `observations.get_many` paginate successfully → export completes
  with full data; total failure → placeholder + incomplete; normal traces still take the fast path
  unchanged (existing `test_langfuse_export.py` green).

No UI test runner is involved — the export change is backend-only; the existing UI export button is
untouched.

## 11. Rollout — two plans

- **Plan 1 (model-side):** `truncate_text` helper, `is_context_overflow`, `trim_messages_to_budget`,
  `ContextOverflowMiddleware`, `ContextOverflowError` + `LLMErrorHandling` unwrap, `ToolOutputCapMiddleware`,
  config, lead + sub-agent wiring, tests.
- **Plan 2 (telemetry-side):** `_truncating_mask`, resilient `_fetch_trace_resilient`, config, tests.
  Depends on `truncate_text` from Plan 1 (or lands the helper first).

## 12. Deferred follow-ups

- Trace-level aggregate byte budget for telemetry (bounds the pathological many-near-cap-observations
  run that a per-field cap doesn't).
- Optionally persist the durable trim into state (not just per-call) when a single giant message keeps
  forcing per-turn re-trims — likely subsumed by Layer 1 in practice.
- Surface a run-level warning in the UI when an export was degraded (truncated/skipped traces).
- Consider spilling a truncated tool output's full content to the workspace (e.g. `outputs/`) so the
  agent can open it, rather than only pointing it at a re-run.
