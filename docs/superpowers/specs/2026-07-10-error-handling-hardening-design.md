# Error-Handling Hardening & Config-Driven Retry — Design Spec

**Date:** 2026-07-10
**Status:** Approved (design), pending implementation plan
**Branch (planned):** `feat/error-handling-hardening`

## Goal

Make atom resilient to transient provider errors — especially Google Gemini's
"Provider is busy" / `503 UNAVAILABLE` / `429 RESOURCE_EXHAUSTED` — by retrying with
exponential backoff (up to 20 attempts) before **failing** the workflow, and close every
other confirmed error-handling gap so a transient hiccup can never silently corrupt a run,
destroy conversation history, or leave a run stuck.

Also: remove DeerFlow branding from user-facing surfaces (the README and the three
"DeerFlow-style" description strings).

## Background — the audit

An adversarially-verified audit (5 finder dimensions, one skeptic per finding) confirmed
**13 distinct issues** (19 findings; several duplicated across dimensions) and refuted 1.
Key structural facts:

- The existing `LLMErrorHandlingMiddleware` (`src/atom/middleware/llm_error.py`) already does
  exponential-backoff retry, but: it is **hardcoded** (`max_retries=2`, `base_delay=1`,
  `max_delay=8`), wired with **no args** at `agent.py:274`; on exhaustion it **returns a
  fallback `AIMessage`** ("I couldn't reach the model…") instead of raising.
- Because it returns a normal message, the workflow engine records the task as
  **`succeeded`** and feeds the apology text into the next step — a silent corruption
  (finding C1).
- Compaction (`SummarizationMiddleware`) calls the summarizer **directly**, bypassing the
  retry middleware, catches any exception into the string `"Error generating summary: …"`,
  and then **unconditionally** `RemoveMessage(ALL)` — permanently replacing history with the
  error string (finding C2).
- Sub-agents (`subagent.py::_child_middleware`) never include the retry middleware at all.
- Gemini error detection relies on `getattr(exc, "status_code")`, but google-genai errors
  carry `.code` (and the langchain wrapper carries neither) — retry currently fires only
  because the numeral coincidentally appears in `str(exc)`.

## Locked decisions

1. **On exhaustion → fail the task.** After the retry budget is spent (or on a non-retryable
   error), the middleware **raises** `ProviderUnavailableError`. In a workflow this fails the
   task → the step halts → the run halts, with the retry history recorded. (Replaces today's
   degrade-to-message behavior.)
2. **Backoff schedule:** 20 retries, `base_delay=1s`, `max_delay=30s`, **full jitter**
   (`sleep = random.uniform(0, min(base * 2**attempt, cap))`). All config-driven.
3. **Compaction on true exhaustion → skip and keep messages.** Never commit a broken summary;
   keep the full history, retry compaction at the next trigger. (Prevents history loss.)
4. **Scope: fix all confirmed issues** — 2 critical + 8 important + 3 minor — in this branch.

## Architecture

One coherent hardening pass across the retry surface, its coverage, the workflow engine's
lifecycle guards, and the CLI entry points. The centerpiece is a small **shared retry core**
in `llm_error.py` reused by (a) the model-call middleware, (b) a retrying wrapper for the
summarizer's out-of-band calls, and (c) the title call site. The middleware *only ever
raises*; each entry point decides how to present the failure.

```
config.retry (RetryConfig)  ──►  RetryPolicy
        │
        ├─► LLMErrorHandlingMiddleware(policy)   (lead agent + sub-agent chains)
        ├─► RetryingModel(summarizer, policy)    (compaction — out-of-band call)
        └─► run_with_retry_*(title_call, policy) (title — best-effort)

exhaustion / non-retryable  ──►  raise ProviderUnavailableError
        │
        ├─ workflow: _run_task except → ts.status="failed" → run halts
        ├─ atom run: try/except → clean [red]Error[/red] + exit 1
        └─ atom chat: try/except per turn → clean error, REPL stays alive
```

## Components (exact changes)

### 1. `src/atom/middleware/llm_error.py` — shared retry core (rewrite/expand)

New public surface (keep the module name so existing imports hold):

- `class ProviderUnavailableError(Exception)` — carries `original: Exception` and
  `attempts: int`; `str()` → `"provider unavailable after N attempt(s): <Type>: <msg>"`.
- `@dataclass RetryPolicy(max_retries: int, base_delay: float, max_delay: float, jitter: bool)`.
- `def is_retryable(exc: Exception) -> bool` — improved detection (see below).
- `def run_with_retry_sync(call: Callable[[], T], policy: RetryPolicy, *, sleep=time.sleep, rand=random.uniform) -> T`
- `async def run_with_retry_async(acall, policy, *, sleep=asyncio.sleep, rand=random.uniform) -> T`
  Both loop `for attempt in range(max_retries + 1)`: call; on success return; on exception, if
  `attempt >= max_retries or not is_retryable(exc)` → `raise ProviderUnavailableError(exc, attempt+1)`;
  else sleep `rand(0, min(base_delay * 2**attempt, max_delay))` (or the undithered value when
  `jitter=False`) and continue. `sleep`/`rand` are injectable for deterministic tests.
- `class RetryingModel` — thin proxy wrapping a `BaseChatModel`: `.invoke(*a, **k)` →
  `run_with_retry_sync(lambda: inner.invoke(*a, **k), policy)`; `.ainvoke` → async variant;
  `__getattr__` delegates everything else to the inner model. Used to give out-of-band
  summarizer calls the same retry policy.
- `class LLMErrorHandlingMiddleware(AgentMiddleware)` — `__init__(self, policy: RetryPolicy | None = None)`
  (defaults to `RetryPolicy(20, 1.0, 30.0, True)` when None, so a bare `LLMErrorHandlingMiddleware()`
  still works). `wrap_model_call` → `return run_with_retry_sync(lambda: handler(request), self.policy)`;
  `awrap_model_call` → async variant. **No more `_fallback` AIMessage** — exhaustion raises.

**Improved `is_retryable`:**
- `status = getattr(exc, "status_code", None) or getattr(exc, "code", None)` — covers
  Anthropic/OpenAI (`status_code`) **and** google-genai (`code`).
- Retry if `status == 429 or (isinstance(status, int) and status >= 500)` — a range test, so
  Anthropic's `529 OverloadedError` and any 5xx are caught structurally.
- `isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.TransportError))` → True
  (covers Gemini's un-wrapped network failures whose `str(exc)` is often empty).
- Extend `_RETRYABLE_MARKERS` with the Google status-enum names and generic terms:
  `"resource_exhausted", "resource exhausted", "unavailable", "internal", "deadline",
  "overloaded", "busy", "quota", "try again"` (in addition to the existing digit/phrase markers).

### 2. `src/atom/config/schema.py` — `RetryConfig`

```python
class RetryConfig(_Base):
    max_retries: int = 20
    base_delay: float = 1.0     # seconds; first backoff
    max_delay: float = 30.0     # seconds; per-attempt cap
    jitter: bool = True         # full jitter on every delay
```

Add `retry: RetryConfig = Field(default_factory=RetryConfig)` to `AtomConfig`. A helper
`RetryConfig.as_policy() -> RetryPolicy` (or a free function in `llm_error`) converts it.
Global policy (not per-profile) — matches "20 retries" as one setting; per-profile override is
a future YAGNI.

### 3. `src/atom/agent.py` — thread the policy, protect the summarizer

- In `_build_middlewares`: build `policy = cfg.retry.as_policy()`; pass to
  `LLMErrorHandlingMiddleware(policy)` (line ~274).
- In `_build_summarizer`: return `RetryingModel(model, policy)` so **both** the compaction
  middleware and `TitleMiddleware` inherit retry. (The main agent still uses the unwrapped
  `prepared.model` with middleware retry — no double-wrap.) `_build_summarizer` needs the
  policy passed in.
- Pass `policy` into `SubagentRunner(...)` (new field) so children get the same treatment.

### 4. `src/atom/subagent.py` — sub-agent retry parity

- Add a `retry: RetryPolicy | None = None` field to `SubagentRunner`.
- In `_child_middleware`: import and prepend `LLMErrorHandlingMiddleware(self.retry)` at the
  outermost `wrap_model_call` position (mirroring `agent.py`), so a delegated child's model
  calls get identical retry/backoff.
- The summarizer handed to child `build_compaction_middleware` is already wrapped when
  `SubagentRunner.summarizer` is a `RetryingModel` (agent.py wraps it before constructing the
  runner) — no extra change needed there, but confirm the runner is given the wrapped model.
- On a child model exhaustion, `ProviderUnavailableError` propagates out of `agent.ainvoke`
  and is caught by the existing `except Exception` in `run()` → returns
  `"[sub-agent '…' failed: provider unavailable after N attempt(s): …]"` to the lead. (The lead
  then makes its own call; if the outage is real, the lead's call also exhausts → raises →
  the run halts. Sub-agents don't unilaterally halt the run.)

### 5. `src/atom/middleware/compaction.py` — fail-closed on summary failure

- `PinnedSummarizationMiddleware`: after `super().before_model()/abefore_model()` returns a
  result, inspect the injected summary message(s) for the langchain sentinel
  `"Error generating summary:"`. If present → the summary genuinely failed (even after the
  `RetryingModel`'s retries) → **return `None`** (skip compaction this turn; keep the original
  messages) instead of committing the destructive `RemoveMessage(ALL)` + broken summary.
- Add a small helper `_summary_failed(result) -> bool` that scans `result["messages"]` for a
  message whose text contains the sentinel. Applied in both sync and async paths, *before* the
  pin injection.
- `build_compaction_middleware` is unchanged in signature; it simply receives a `RetryingModel`
  now (so its internal `model.invoke` retries transient blips before the sentinel path is ever
  reached).

### 6. `src/atom/middleware/title.py` — best-effort, retries via the wrapped summarizer

`TitleMiddleware` is constructed as `TitleMiddleware(summarizer)` in `agent.py`. Once
`_build_summarizer` returns a `RetryingModel` (§3), title's `self.model.invoke([...])` is
**already retried** with the same policy — a transient blip is absorbed, and on true exhaustion
`ProviderUnavailableError` is caught by the existing `except Exception: return None`, so title
stays best-effort and self-heals on the next turn. **No code change to `title.py`** beyond a
one-line comment noting retry comes from the wrapped summarizer (avoids a double-retry wrap).
This closes finding M11 for free.

### 7. `src/atom/models/registry.py` — single retry authority + per-call timeout

- In `build_model`: `kwargs.setdefault("max_retries", 1)` so the provider SDK's own retry layer
  is disabled (Gemini default is 6; 1 disables it — `0` means "Google default 5", so use `1`)
  and `LLMErrorHandlingMiddleware` is the single, predictable retry authority across providers.
- `kwargs.setdefault("timeout", DEFAULT_REQUEST_TIMEOUT_SECONDS)` where
  `DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0` — a per-call backstop so a genuinely stalled
  connection fails fast (and is retried by the middleware) instead of holding a concurrency slot
  for the full 1800s task timeout. Fixed default (overridable via `overrides`); not config-driven
  (YAGNI). Applied to both the `init_chat_model` path and the Qwen `ChatQwen` path.

### 8. `src/atom/workflow/engine.py` — lifecycle guards

- **Guard the initial load:** wrap `manifest = self.store.load(run_id)` (currently *outside* the
  `try`) so a load failure logs loudly (`logger.exception`) and does not vanish silently. If the
  manifest can't be loaded there is nothing to mark; log and re-raise.
- **Log the silent paths:** add `logger.exception(...)` before the bare `except Exception: pass`
  in `_on_task_done` and before the best-effort `save()` in the `except BaseException` block, so
  a run that ends up stuck/halted is discoverable in logs.
- **Cancellation:** add `except asyncio.CancelledError:` in `_run_task` (before `except Exception`)
  that sets `ts.status = "failed"`, `ts.error = "cancelled"`, `ts.ended_at`, best-effort saves,
  then **re-raises** — so a cancelled run leaves clean terminal per-task state instead of zombie
  `running` entries. (The exhaustion→raise change means the common provider-outage path now
  arrives here as a normal `ProviderUnavailableError` and is failed correctly by the existing
  `except Exception`.)

### 9. `src/atom/cli.py` — clean errors on the single-shot paths

- Change the app help string `"atom — a DeerFlow-style agentic harness."` →
  `"atom — an agentic harness."`.
- Wrap the `asyncio.run(run_agent(...))` call in `run()` with a `try/except` that catches
  `ProviderUnavailableError`, pydantic `ValidationError`, `KeyError` (unknown profile/model),
  and `FileNotFoundError` (bad `--config`) → `console.print("[red]Error: …[/red]")` +
  `raise typer.Exit(1)` (mirroring the existing `workflow run`/`workflow export` pattern).
- In `chat()`: wrap the per-turn `asyncio.run(run_agent(...))` in the same `try/except`, but
  **print the error and `continue`** (keep the REPL alive) instead of exiting.

### 10. DeerFlow branding scrub

- `README.md:3` — "A DeerFlow-style **agentic middleware harness**" → "An **agentic middleware harness**".
- `pyproject.toml:8` — `description = "atom — a DeerFlow-style agentic middleware harness on LangChain v1"`
  → `"atom — an agentic middleware harness on LangChain v1"`.
- `src/atom/__init__.py:1` — docstring "atom — a DeerFlow-style agentic middleware harness built on
  LangChain v1." → drop "DeerFlow-style".
- `src/atom/cli.py:21` — covered in §9.
- **Leave `inspo.md` untouched** — it is a design study *about* DeerFlow, not atom branding.

## Data flow — the failure path (worked example)

Gemini `gemini-flash` task, provider returns `503 UNAVAILABLE` persistently:

1. `model_node` calls the model; `awrap_model_call` runs it via `run_with_retry_async`.
2. `is_retryable` → True (`code == 503` ≥ 500, and/or "unavailable" marker). Sleep
   `uniform(0, min(1*2**attempt, 30))`, retry. Each attempt is its own LangSmith child run.
3. After 20 retries still failing → `raise ProviderUnavailableError(orig, 21)`.
4. Propagates out of `agent.ainvoke` → out of `run_agent` → `_run_task`'s `except Exception`:
   `ts.status="failed"`, `ts.error="provider unavailable after 21 attempt(s): …"`.
5. `compute_step_status` → step `failed` → `manifest.status="halted"`, `ended_at` set, saved.
6. The run halts honestly; the full retry history is in LangSmith + the error in `run.json`.

## Error-handling semantics summary

| Path | Transient error (within budget) | Exhausted / non-retryable |
|------|--------------------------------|---------------------------|
| Lead model call | retry w/ backoff+jitter | raise → task failed → run halts |
| Sub-agent model call | retry w/ backoff+jitter | raise → caught in `run()` → failure string to lead |
| Compaction summary | `RetryingModel` retries | sentinel detected → skip compaction, keep history |
| Title | inherits `RetryingModel` retry | raise → caught → no title this turn (self-heals) |
| `atom run` | (retried below) | clean `[red]Error[/red]` + exit 1 |
| `atom chat` | (retried below) | clean error, REPL continues |
| Run cancelled | — | clean terminal task/step state, re-raise |

## Testing

New/updated tests (run via `.venv/bin/python -m pytest`):

- `tests/test_llm_error.py` (new): `is_retryable` truth table across simulated Anthropic
  (`status_code=529`), google-genai (`code=503`, `code=429`), httpx timeout/connect, wrapped
  `GoogleGenerativeAIError` (string-only), and non-retryable (400) exceptions;
  `run_with_retry_sync/async` — success first try, success after N, exhaustion raises
  `ProviderUnavailableError` with correct `attempts`, jitter bounds (with injected `rand`/`sleep`),
  non-retryable raises immediately; `RetryingModel` proxies `.invoke/.ainvoke` with retry and
  delegates other attrs; `LLMErrorHandlingMiddleware` raises on exhaustion (a fake handler that
  always raises a retryable error, injected sleep).
- `tests/test_compaction.py` (extend): a summarizer stub whose invoke raises transiently then
  succeeds → compaction proceeds; a summarizer that always fails → `RetryingModel` exhausts →
  langchain sentinel → `before_model` returns `None` (messages preserved, no `RemoveMessage`).
- `tests/test_subagent.py` (extend): `_child_middleware()` includes `LLMErrorHandlingMiddleware`;
  a child whose model raises a retryable error is retried (injected sleep) and, on exhaustion,
  yields the `[sub-agent … failed: provider unavailable …]` string.
- `tests/test_workflow_engine.py` (extend): a task whose model raises `ProviderUnavailableError`
  → `ts.status="failed"`, run `halted`; a `CancelledError` mid-task leaves clean terminal state
  (no zombie `running`); an initial `store.load` failure logs and re-raises.
- `tests/test_models_registry.py` (extend): `build_model` sets `max_retries=1` and a `timeout`
  in the kwargs passed to `init_chat_model` (patch `init_chat_model`, assert kwargs).
- `tests/test_cli.py` (extend/new): `atom run` with a `run_agent` that raises
  `ProviderUnavailableError` → exit code 1 + a red error line (CliRunner); `atom chat` keeps the
  REPL alive on the same error.
- Config: `RetryConfig` defaults + `as_policy()` mapping.
- A grep-style guard test (optional) asserting no "DeerFlow-style" remains in README/pyproject/
  `__init__`/`cli` help.

## Out of scope (deferred / YAGNI)

- Per-profile retry overrides (global `retry:` block only for now).
- Config-driven per-call `timeout` (fixed 120s default suffices).
- Retrying the *whole* `agent.ainvoke` in `SubagentRunner.run` for non-model-node failures
  (the audit's defense-in-depth suggestion) — the middleware covers model-node errors, which is
  the real case.
- Changing `task_timeout_seconds` semantics.

## Traceability (finding → fix)

| # | Severity | Finding | Fixed in |
|---|----------|---------|----------|
| C1 | critical | exhausted retry recorded as `succeeded` | §1 (raise), §8 (fail) |
| C2 | critical | compaction destroys history on transient error | §5 (+§3 wrapped summarizer) |
| I3 | important | retry policy hardcoded | §1, §2, §3 |
| I4 | important | sub-agents no retry coverage | §4 |
| I5 | important | Gemini detection wrong attribute | §1 (`is_retryable`) |
| I6 | important | Anthropic 529 not retryable | §1 (range test) |
| I7 | important | raw httpx timeouts unmatched | §1 (isinstance) |
| I8 | important | SDK double-retry stacking | §7 (`max_retries=1`) |
| I9 | important | unguarded initial load / silent stuck run | §8 |
| I10 | important | CancelledError → zombie running state | §8 |
| M11 | minor | title no retry | §6 (via wrapped summarizer) |
| M12 | minor | no per-call model timeout | §7 (`timeout=120`) |
| M13 | minor | CLI raw traceback on missing key | §9 |
| — | — | DeerFlow branding | §10 |
