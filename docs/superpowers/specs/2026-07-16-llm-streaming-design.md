# Live LLM streaming to the frontend — design

- **Date:** 2026-07-16
- **Status:** Draft (brainstorm), pending user review → implementation plan
- **Scope:** Stream the lead agent's thinking, assistant text, tool calls, and tool results to the `atom-ui` run view **as a task executes**, instead of showing an empty transcript until the task finishes. Additive, in-process, config-gated. The durable run record is unchanged; streaming rides alongside it.

## Problem

A workflow task runs via `run_agent` → `await agent.ainvoke(...)` (`src/atom/runtime.py:126`) — one long await that returns only the final state. Two things then hide all intermediate progress from a user watching the run:

- **Backend:** the transcript is persisted **once, at task completion** — `store.save_chat(...)` fires only after the awaited result returns (`src/atom/workflow/engine.py:404`). No incremental output is ever emitted while the model is thinking, writing, or calling tools.
- **Frontend:** `RunView` polls `GET /api/runs/{id}` every 1.5s for *status* only (`atom-ui/src/RunView.tsx:77-91`), and the `Transcript` component re-fetches `GET /api/runs/{id}/tasks/{step}/{task}/messages` **only when the run's overall status changes** (`RunView.tsx:235`, `status` in the effect deps). So during a long task the user watches a spinning dot over an empty/stale transcript, with no visibility into what the agent is doing.

Long tasks are exactly where this hurts: an atom task routinely spends minutes on reasoning + many tool calls (bash/read/edit) + delegated sub-agents. The user's goal: **someone following a run should see thinking and output appear incrementally.** The constraint: **efficient and resource-efficient.**

## Why this is cleanly solvable here

Two structural facts make an in-process streaming path the natural fit:

1. **Single process, one event loop.** `atom serve` is `uvicorn.run(create_app(...))` with **no `workers=` argument and an app *instance*** (`src/atom/cli.py:390`) → a single uvicorn process. That one process serves the UI/API, owns the durable-queue worker (started in the API lifespan under the worker lease, `src/atom/api/app.py:68-79`), and executes every task's `agent` on the **same asyncio event loop** (`engine.execute` → `asyncio.gather` over `_run_task` → `await run_agent`). Therefore an executing task and a streaming HTTP endpoint can share an **in-process async pub/sub** — no message broker, no second worker process, no cross-process transport.
2. **LangChain v1 supports token-level streaming as a drop-in.** `agent.astream(stream_mode="messages")` yields `(chunk, metadata)` where `chunk.content_blocks` carries **both reasoning/thinking and text deltas**; `agent.astream_events(version="v3")` exposes the same as typed events plus tool start/end and a recoverable final `output`. Either can replace `ainvoke` while still reconstructing the final state, so `run_agent`'s return contract is preserved.

## Locked decisions (brainstorming)

1. **Stream scope = full lead-agent transcript.** Thinking + assistant text deltas **plus** tool calls and tool results as they happen, for the **lead** agent. This is what makes "following along" useful — you watch it read files, run bash, and write.
2. **Persistence = in-memory live + existing on-disk snapshot.** Live deltas are held in memory only; the existing `save_chat` on-disk snapshot remains the durable source of truth. Chosen for resource-efficiency (no disk churn at token cadence).

> Both decisions were selected as defaults while the user was away, aligned with the stated goal ("a user following along cannot see what is happening") and constraint ("efficient and resource efficient"). Adjustable at spec review.

## Non-goals / deferred (explicit)

- **Sub-agent *internal* token streaming.** A `delegate_task` call still surfaces live as a `tool_call` event and then its `tool_result` (the sub-agent's final text) — the user sees the delegation happen and its outcome, just not the sub-agent's own token stream. Nested streaming (routing a child runner's events up through the parent channel) is a **follow-up**, noted in §Follow-ups.
- **Durable on-disk event log.** Rejected in favor of in-memory (decision 2). If durable replay across server restarts is later required, the on-disk-tail variant (§Alternatives) is the upgrade path.
- **Multi-process / multi-worker serving, and CLI-driven runs.** The in-memory bus assumes the SSE endpoint is served by the *same* process that executes the task — **true today** for `atom serve` (single-process, and it holds the worker lease so it drains the runs it serves). Two cases fall outside and are **not** solved here: (a) `uvicorn --workers >1`; (b) a run launched via CLI `atom workflow run`, which executes in the *CLI* process (`engine.await_run`, `engine.py:257-274`), invisible to the API process's in-memory bus. The UI targets `atom serve`-drained runs, so this is acceptable; the on-disk-tail transport (§Alternatives) is the upgrade path if either case must stream.

## Architecture (end-to-end)

### 1. `RunEventBus` — new module `src/atom/workflow/events.py`

An in-memory, per-task async pub/sub. **One channel per `(run_id, step_index, task_id)`.**

- **Channel state:** a set of subscriber `asyncio.Queue`s + a **bounded accumulator** (the in-progress transcript so far) + a `closed` flag + a terminal marker/`error`.
- `publish(channel, event)` — append to the accumulator (with **coalescing**: consecutive `text_delta`/`thinking_delta` merge into the trailing block rather than growing an unbounded list), then fan out to each subscriber queue **non-blocking**. On a full queue, drop and set a `lagged` flag on that subscriber (bounds memory; the subscriber is told to re-sync).
- `subscribe(channel) -> AsyncIterator[Event]` — registers a queue; **first yields a `snapshot` event built from the accumulator** (catch-up), then yields live events until the channel closes and its queue drains.
- `close(channel, error=None)` — set `closed`/`error`, push a terminal sentinel to all subscribers, and schedule accumulator eviction after a short TTL (so a refresh landing just after completion still catches up, then the client falls back to the durable snapshot).
- **Memory bounds:** coalesced text + a capped count of tool events in the accumulator; when a text block exceeds `accumulator_max_chars`, older text is collapsed/elided (head+tail kept). Per-subscriber queue is bounded (`subscriber_queue_max`). Channels are removed after close+TTL. A module-level singleton (attached to the engine) holds the channel map; valid because serving is single-process.

**Isolation:** the bus knows nothing about LangChain, HTTP, or disk — it moves opaque event dicts between one producer and N consumers. Testable in isolation with plain `asyncio.Queue`s.

### 2. Streaming in `run_agent` — `src/atom/runtime.py`

- New optional param `on_event: Callable[[dict], None] | None = None`. When `None` (the CLI path, tests that don't stream), behavior is unchanged in spirit and the existing `RunResult` is returned; no fan-out cost.
- When `on_event` is provided (and `cfg.streaming.enabled`), replace the single `await agent.ainvoke(...)` with an `async for` over the agent stream, translating provider chunks into **atom-level, provider-agnostic events** and calling `on_event` for each. The **final state must be reconstructed from the stream**, because `run_agent`'s downstream logic depends on the terminal messages+state (`final_text` extraction `runtime.py:150-156`, `pending_clarification` detection `runtime.py:132-148`, `state["artifacts"]` consumed at `engine.py:407`). `stream_mode="messages"` yields token chunks but **not** the final graph state, so use the **combined `stream_mode=["messages","values"]`** (the `values` stream's last item is the terminal state) — or `astream_events(version="v3")` whose `.output` is the final state. Either way `RunResult{thread_id, messages, final_text, state, awaiting_clarification}` is produced **exactly as today**; only its input now comes from the stream instead of `ainvoke`.
- **Sub-agent event filtering (important).** A delegated sub-agent runs its own `create_agent` graph via `SubagentRunner.run` → `ainvoke` inside the `delegate_task` tool node (`src/atom/subagent.py:184`). Because LangChain propagates callbacks through `RunnableConfig`/contextvars in async contexts, the sub-agent's **model deltas can surface as nested events in the lead's stream**. For v1 (lead-only scope) the translator **emits only lead-graph model deltas and filters out nested sub-agent deltas by metadata** (`metadata["langgraph_node"]` / a subagent tag / event depth) — otherwise up to 3 concurrent sub-agents × up to 4 parallel tasks would interleave unattributed. This filter is precisely the seam the deferred sub-agent-streaming follow-up would open up (route nested events up with attribution) rather than drop.
- **atom event model** (the wire contract for the bus and SSE):
  - `message_boundary` `{role}` — a new assistant/tool turn began (UI starts a fresh bubble).
  - `thinking_delta` `{text}` — incremental reasoning text (from `content_blocks` reasoning/thinking blocks).
  - `text_delta` `{text}` — incremental assistant text (from `content_blocks` text blocks).
  - `tool_call` `{id, name, args}` — an assistant tool call finalized in the turn.
  - `tool_result` `{tool_call_id, name, text, is_error}` — a `ToolMessage` result.
  - `snapshot` `{blocks}` — synthesized by the bus for late joiners (not emitted by `run_agent`).
  - `done` `{status}` / `error` `{message}` — synthesized by the bus/SSE on channel close.
- **Thinking vs text** is distinguished by inspecting `content_blocks` block `type` on each `AIMessageChunk` (reasoning/thinking vs text), consistent with `messages.py::message_text`. Exact astream API (`stream_mode="messages"` tuple form vs `astream_events` typed form) is finalized in the implementation plan; this spec fixes the event *model*, not the LangChain call shape.
- When `cfg.streaming.enabled` is `False`, `run_agent` keeps using `ainvoke` and ignores `on_event`.

### 3. Engine wiring — `src/atom/workflow/engine.py::_run_task`

- Before the `run_agent` call, open the channel `bus.channel(run_id, step_index, task_id)` and build an `emit` adapter (does the ~`coalesce_ms` text batching, then `bus.publish`). Pass `on_event=emit` into `run_agent`.
- In a `finally`, `bus.close(channel, error=...)` so subscribers always terminate, on success, failure, timeout, or cancellation. This slots into the existing try/except in `_run_task` (which already guarantees the method never raises) — streaming close is best-effort and must never mask a task result.
- **Everything else in `_run_task` is untouched:** it still `save_chat`s the final serialized messages, captures artifacts, and writes task status. The durable record and existing polling behavior are byte-for-byte the same; streaming is purely additive.
- The `WorkflowEngine` gains a `self.bus = RunEventBus(...)` (constructed from `cfg.streaming`), so the API layer (which already holds the `engine`) can reach the same bus instance.

### 4. SSE endpoint — `src/atom/api/app.py`

`GET /api/runs/{run_id}/tasks/{step}/{task_id}/stream` → a `StreamingResponse(media_type="text/event-stream")` (Starlette; async generator).

- Subscribe to `engine.bus.channel(run_id, step, task)`. Emit the initial `snapshot` event, then live events as SSE frames (`event: <type>\ndata: <json>\n\n`), with periodic heartbeat comments (`: ping`) every `heartbeat_seconds` to keep intermediaries from closing an idle connection.
- On channel close, emit a final `done` (or `error`) event and end the response. The client then does **one authoritative fetch** of `.../messages` (the completed on-disk chat = source of truth) and stops streaming.
- **Edge cases:** task already terminal / channel already evicted → emit `done` immediately (client falls back to the messages endpoint). Task not yet started (no channel) → subscribe-and-wait up to a short bound, or return `204`/`done` so the client retries on the next manifest poll. **Client disconnect** → the async generator is cancelled → `finally` unsubscribes and cleans up the queue.
- No auth (consistent with the rest of the API; CORS already allow-all). When `cfg.streaming.enabled` is `False`, the endpoint returns `404` so the frontend cleanly falls back to polling.

### 5. Frontend — `atom-ui/src/{api.ts, RunView.tsx, styles.css}`

- `api.ts`: add `streamUrl(id, step, task)`; extend the transcript model with a lightweight live shape (ordered blocks: `thinking` | `text` | `tool_call` | `tool_result`, each with a `streaming` flag).
- A `useTaskStream(runId, sel, taskStatus)` hook opens **one** native `EventSource` when the selected task is `running`, folds incoming events into the live block list, and on `done`/terminal closes the connection and triggers the existing `api.messages(...)` fetch for the authoritative final transcript. Switching the selected task closes the old `EventSource` and opens the new one → **at most one open connection per viewer**. Native `EventSource` auto-reconnect covers a mid-run server restart (the task re-queues and re-streams on a fresh channel).
- `Transcript` renders the **live** blocks while streaming (blinking caret on the actively-streaming block; thinking shown in a subdued/collapsible block distinct from assistant text; tool calls/results reuse the existing `.toolcall`/`.msg.tool` styling), and the **fetched snapshot** otherwise — identical to today for completed tasks.
- `styles.css`: a caret/`@keyframes` blink + a `thinking` block style. Light-only, matching the existing single `:root` palette. (Frontend/UX details are decided here per the standing split.)
- Manifest polling stays for the rail/status; its interval may be relaxed while a stream is open since progress is visible via the stream.
- **Dev proxy:** the Vite dev server proxies `/api` → `127.0.0.1:8000` (`atom-ui/vite.config.ts`); the SSE route must pass through **un-buffered** (SSE works through Vite's http-proxy, but verify no response buffering/compression on the stream path). In production the SPA is served from the same origin as the API (`app.mount("/")`), so no proxy is involved.

### 6. Config — `streaming:` block

Add `StreamingConfig` to `src/atom/config/schema.py` and a `streaming:` block to `config.yaml`:

- `enabled: true` — master switch. `False` ⇒ `run_agent` uses `ainvoke`, the SSE endpoint 404s, and the frontend falls back to today's polling. **Full graceful degradation.**
- `coalesce_ms: 50` — text/thinking delta batching interval (fewer, larger fan-outs).
- `accumulator_max_chars` / `subscriber_queue_max` — memory bounds (§1).
- `heartbeat_seconds: 15` — SSE keep-alive.

Consistent with atom's config-driven ethos ([[atom-user-preferences]]).

## Data flow (happy path)

1. Browser opens `RunView`, sees the selected task `running`, opens `EventSource(.../stream)`.
2. SSE endpoint subscribes to the bus channel, sends the accumulated `snapshot` (catch-up), then live events.
3. The task's `run_agent` astream emits `thinking_delta`/`text_delta`/`tool_call`/`tool_result` → `emit` (coalesce) → `bus.publish` → SSE → browser renders incrementally.
4. Task completes → `_run_task` runs `save_chat` (durable) + `bus.close`.
5. SSE sends `done` → browser closes `EventSource` and fetches `.../messages` for the authoritative final transcript; the manifest poll flips the task dot to `succeeded`.

## Error handling

- **astream raises mid-stream** → `_run_task`'s existing `except` marks the task `failed`; the `finally` calls `bus.close(channel, error=...)`; SSE relays `error`; the frontend shows failure with whatever streamed so far. (On failure there is no `save_chat` snapshot today either — unchanged behavior.)
- **Client disconnect** → SSE generator cancelled → unsubscribe + queue cleanup.
- **Slow/absent consumer** → bounded subscriber queue drops with a `lagged` flag; the frontend re-syncs by re-subscribing (fresh `snapshot`).
- **Server restart mid-run** → in-memory bus is lost, but the engine already re-queues + re-runs the interrupted task (`engine.recover`), producing a new channel; the browser's `EventSource` auto-reconnects and re-streams. No durability gap for the live-watching use case.
- **Memory** is bounded by coalescing + accumulator caps + channel eviction TTL after close.

## Testing

- **Unit — `RunEventBus`:** subscribe yields snapshot-then-live; late subscriber catch-up; `close` terminates all subscribers; bounded-queue drop sets `lagged`; multi-subscriber fan-out; accumulator coalescing + cap/elision; channel eviction after TTL.
- **Unit — event translation:** feed a **fake astream** (thinking blocks, text blocks, tool-call chunks, ToolMessage) through `run_agent` with a `prepared` model (the suite already injects `prepared` to avoid real providers) and assert the emitted atom-event sequence.
- **Contract — `run_agent`:** with `on_event` set, the returned `RunResult` (messages/final_text/state/awaiting_clarification) equals the `ainvoke` path for the same fake model (streaming must not change the result).
- **API — SSE:** endpoint returns `text/event-stream`; emits `snapshot` then `done`; terminal-task and unknown-task fast-paths; disconnect cleanup; `404` when `streaming.enabled=false`.
- **Integration/e2e:** a short streamed task (fake or a real haiku run) asserts that `text_delta` events are observed **before** the task reaches a terminal status.
- **Frontend:** minimal (per the standing split) — assert fallback-to-polling when the endpoint 404s, and that switching tasks closes the prior `EventSource`.

## Alternatives considered

- **B — SSE tailing an on-disk NDJSON event log per task.** Durable replay across server restart / cross-process and a persisted incremental transcript for free, but disk writes at token cadence (even coalesced) cut against the resource-efficiency constraint. Kept as the documented upgrade path if durability/multi-process is later needed.
- **C — Faster polling of partial `save_chat` snapshots.** Simplest (no new transport), but not real streaming — chunky and laggy, and it rewrites the entire transcript file repeatedly. Rejected.

## Files touched

| File | Change |
|---|---|
| `src/atom/workflow/events.py` | **new** — `RunEventBus` (channels, accumulator, subscribe/publish/close) |
| `src/atom/runtime.py` | `run_agent`: `on_event` param; `astream`/`astream_events` path; final-state reconstruction preserving `RunResult` |
| `src/atom/workflow/engine.py` | `WorkflowEngine.bus`; `_run_task` opens channel, passes `emit`, closes in `finally` |
| `src/atom/api/app.py` | new `GET …/stream` SSE endpoint; 404 when disabled |
| `src/atom/config/schema.py` | `StreamingConfig` (+ wire into `AtomConfig`) |
| `config.yaml` | `streaming:` block |
| `atom-ui/src/api.ts` | `streamUrl` + live transcript types |
| `atom-ui/src/RunView.tsx` | `useTaskStream` hook; live rendering in `Transcript` |
| `atom-ui/src/styles.css` | streaming caret + thinking block styles |
| `atom-ui/vite.config.ts` | verify the `/api` dev proxy passes SSE un-buffered (likely no change; confirm) |

## Follow-ups (out of scope)

- **Sub-agent internal streaming** — route a delegated child runner's events up through the parent's channel (nested block grouping in the UI).
- **On-disk-tail transport (approach B)** — enable durable replay + multi-process serving if needed.
- **Relax manifest polling while a stream is open** — minor efficiency win, easy to add once streaming lands.
