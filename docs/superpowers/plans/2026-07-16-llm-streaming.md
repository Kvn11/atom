# Live LLM Streaming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stream the lead agent's thinking, assistant text, tool calls, and tool results to the `atom-ui` run view live as a task executes, instead of showing an empty transcript until the task finishes.

**Architecture:** Additive, in-process, config-gated. `run_agent` swaps its single `agent.ainvoke` for a streamed drive (`agent.astream(stream_mode=["messages","updates"])` + `aget_state` for the final state), translating LangChain chunks into provider-agnostic atom event dicts. An in-memory `RunEventBus` (one channel per `(run_id, step, task)`) fans those events to a new SSE endpoint the browser consumes via `EventSource`. The durable on-disk chat snapshot is unchanged; on stream close the client refetches it as the source of truth.

**Tech Stack:** Python 3, FastAPI/Starlette, LangChain v1 (`langchain.agents.create_agent`) + LangGraph, asyncio, pytest + pytest-asyncio, httpx `AsyncClient`; React 18 + TypeScript + Vite, native `EventSource`.

## Global Constraints

- **Config-driven ethos:** every knob lives in a `streaming:` config block (`src/atom/config/schema.py` + `config.yaml`); nothing hardcoded. When `streaming.enabled` is `False`, behavior degrades exactly to today's polling.
- **Preserve the `run_agent` contract byte-for-byte:** `RunResult{thread_id, messages, final_text, state, awaiting_clarification}` must be identical whether streaming or not. The clarification-detection and `final_text` paths (`runtime.py:132-157`) are unchanged; only their *input* (the final state) now comes from `aget_state` instead of `ainvoke`.
- **`_run_task` must never raise** (`engine.py:366-433` docstring): all streaming lifecycle calls (`emitter.aclose`, `bus.close`) are best-effort in `try/except` and must never mask a task result.
- **Scope = lead agent only.** Sub-agent *internal* token streaming is deferred; `delegate_task` still surfaces live as a `tool_call` + `tool_result`. Nested sub-agent model deltas are filtered out by a metadata marker.
- **Run the test suite with `.venv/bin/python -m pytest`, NOT `.venv/bin/pytest`** (the suite does `from tests.conftest import ...`; the bare console script drops the repo root from `sys.path`).
- **Single-process assumption:** the in-memory bus assumes the SSE endpoint is served by the same process that executes the task — true for `atom serve`. Not solved: `uvicorn --workers >1` and CLI-driven runs.
- **Frontend is light-only** (single `:root` palette, no dark mode). FE has no test runner (devDeps are `vite`/`typescript` only); FE verification = `npm run build` (typecheck) + manual run.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/atom/config/schema.py` | **Modify** — add `StreamingConfig`; wire `AtomConfig.streaming` |
| `config.yaml` | **Modify** — add the `streaming:` block |
| `src/atom/workflow/events.py` | **Create** — `RunEventBus` + `channel_key` (transport-agnostic in-memory pub/sub; no LangChain/HTTP deps) |
| `src/atom/streaming.py` | **Create** — event-type constants, `translate_message_chunk`/`translate_update` (LangChain chunk → atom events), `StreamEmitter` (coalescing). No transport/bus deps. |
| `src/atom/runtime.py` | **Modify** — `run_agent` gains `on_event`; streamed drive + `aget_state`; contract preserved |
| `src/atom/subagent.py` | **Modify** — `_child_config` tags child runs `atom_subagent` so their deltas are filtered out |
| `src/atom/workflow/engine.py` | **Modify** — `WorkflowEngine.bus`; `_run_task` opens a channel, passes `emitter.emit`, closes in `finally` |
| `src/atom/api/app.py` | **Modify** — new `GET …/stream` SSE endpoint; 404 when disabled |
| `atom-ui/src/api.ts` | **Modify** — `streamUrl` + live block types |
| `atom-ui/src/RunView.tsx` | **Modify** — `useTaskStream` hook + live rendering in `Transcript` |
| `atom-ui/src/styles.css` | **Modify** — streaming caret + thinking-block styles |
| `tests/conftest.py` | **Modify** — add `make_streaming_prepared` text-streaming fake (Task 4) |

Dependency order: Task 1 (config) → Task 2 (bus) → Task 3 (translate/emit) → Task 4 (run_agent) → Task 5 (engine) → Task 6 (SSE) → Task 7 (subagent marker) → Task 8 (frontend). Tasks 2 and 3 are independent of each other; everything else is sequential.

---

### Task 1: Streaming config

**Files:**
- Modify: `src/atom/config/schema.py` (add class after `QueueConfig`, ~line 74; add field to `AtomConfig` ~line 155)
- Modify: `config.yaml` (add block after the `queue:` block)
- Test: `tests/test_workflow_config.py`

**Interfaces:**
- Produces: `StreamingConfig(enabled: bool, coalesce_ms: int, coalesce_chars: int, accumulator_max_chars: int, subscriber_queue_max: int, retain_closed: int, heartbeat_seconds: float)`; `AtomConfig.streaming: StreamingConfig`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_workflow_config.py`:

```python
def test_streaming_config_defaults():
    from atom.config.schema import AtomConfig
    cfg = AtomConfig()
    assert cfg.streaming.enabled is True
    assert cfg.streaming.coalesce_ms == 50
    assert cfg.streaming.coalesce_chars == 240
    assert cfg.streaming.heartbeat_seconds == 15.0


def test_streaming_config_override():
    from atom.config.schema import AtomConfig, StreamingConfig
    cfg = AtomConfig(streaming=StreamingConfig(enabled=False, coalesce_ms=10))
    assert cfg.streaming.enabled is False
    assert cfg.streaming.coalesce_ms == 10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_workflow_config.py::test_streaming_config_defaults -v`
Expected: FAIL with `AttributeError`/`ImportError` (no `StreamingConfig`).

- [ ] **Step 3: Add the config class + field**

In `src/atom/config/schema.py`, after the `QueueConfig` class (~line 74):

```python
class StreamingConfig(_Base):
    # Live-stream the lead agent's thinking/text/tool activity to the run view as a task runs.
    # False -> run_agent uses ainvoke and the SSE endpoint 404s (the UI falls back to polling).
    enabled: bool = True
    # Batch text/thinking deltas over this window (ms) OR this many chars, whichever first, before
    # publishing — bounds SSE frame + React re-render frequency for high-rate token streams.
    coalesce_ms: int = 50
    coalesce_chars: int = 240
    # Per-channel catch-up buffer cap; the trailing text block is elided past this to bound memory.
    accumulator_max_chars: int = 20000
    # Per-subscriber queue depth before deltas are dropped (client re-syncs from a fresh snapshot).
    subscriber_queue_max: int = 512
    # Keep this many completed channels so a subscriber joining just after completion still catches up.
    retain_closed: int = 64
    # SSE keep-alive ping cadence (seconds).
    heartbeat_seconds: float = 15.0
```

In the `AtomConfig` class, add the field alongside the other sub-configs (after `queue:` ~line 151):

```python
    streaming: StreamingConfig = Field(default_factory=StreamingConfig)
```

- [ ] **Step 4: Add the `streaming:` block to `config.yaml`**

After the `queue:` block (~line 34) in `config.yaml`:

```yaml
streaming:
  enabled: true            # stream live thinking/text/tool activity to the run view; false -> UI polls as before
  coalesce_ms: 50          # batch token deltas over this window before pushing (efficiency)
  coalesce_chars: 240      # ...or after this many buffered chars, whichever comes first
  heartbeat_seconds: 15    # SSE keep-alive cadence
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_config.py -v`
Expected: PASS. Also run `.venv/bin/python -c "from atom.config import load_config; print(load_config().streaming.enabled)"` → prints `True` (confirms the yaml block loads).

- [ ] **Step 6: Commit**

```bash
git add src/atom/config/schema.py config.yaml tests/test_workflow_config.py
git commit -m "feat(config): add streaming config block"
```

---

### Task 2: `RunEventBus` — in-memory pub/sub

**Files:**
- Create: `src/atom/workflow/events.py`
- Test: `tests/test_run_event_bus.py`

**Interfaces:**
- Produces:
  - `channel_key(run_id: str, step: int, task_id: str) -> str` (returns `f"{run_id}:s{step}:{task_id}"`)
  - `class RunEventBus(*, max_chars=20000, queue_max=512, retain_closed=64, heartbeat_seconds=15.0)`
    - `async def publish(self, key: str, event: dict) -> None`
    - `async def close(self, key: str, error: str | None = None) -> None`
    - `async def stream(self, key: str) -> AsyncIterator[dict]` — yields a `{"type":"snapshot","blocks":[...]}` first, then live event dicts, then a terminal `{"type":"done"}` or `{"type":"error","message":...}`; yields `{"type":"ping"}` on idle heartbeat.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_run_event_bus.py`:

```python
"""RunEventBus: snapshot-then-live, late-join catch-up, close semantics, bounded buffer."""
from __future__ import annotations

import asyncio

import pytest

from atom.workflow.events import RunEventBus, channel_key


def test_channel_key():
    assert channel_key("r1", 0, "writer") == "r1:s0:writer"


@pytest.mark.asyncio
async def test_subscribe_gets_snapshot_then_live():
    bus = RunEventBus()
    k = "r:s0:t"
    await bus.publish(k, {"type": "text_delta", "text": "he"})
    await bus.publish(k, {"type": "text_delta", "text": "llo"})

    gen = bus.stream(k)
    first = await gen.__anext__()
    assert first["type"] == "snapshot"
    # coalesced into one trailing text block
    assert first["blocks"] == [{"type": "text_delta", "text": "hello"}]

    await bus.publish(k, {"type": "tool_call", "name": "bash", "args": {}})
    live = await gen.__anext__()
    assert live["type"] == "tool_call" and live["name"] == "bash"

    await bus.close(k)
    end = await gen.__anext__()
    assert end["type"] == "done"
    await gen.aclose()


@pytest.mark.asyncio
async def test_late_subscribe_after_close_gets_snapshot_then_done():
    bus = RunEventBus()
    k = "r:s0:t"
    await bus.publish(k, {"type": "text_delta", "text": "hi"})
    await bus.close(k)

    seen = [ev async for ev in bus.stream(k)]
    assert seen[0]["type"] == "snapshot"
    assert seen[0]["blocks"] == [{"type": "text_delta", "text": "hi"}]
    assert seen[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_close_with_error_yields_done_with_error_field():
    bus = RunEventBus()
    k = "r:s0:t"
    await bus.close(k, error="boom")
    seen = [ev async for ev in bus.stream(k)]
    assert seen[-1] == {"type": "done", "error": "boom"}  # terminal is always 'done'; failure carries error


@pytest.mark.asyncio
async def test_accumulator_bounds_trailing_text():
    bus = RunEventBus(max_chars=10)
    k = "r:s0:t"
    for _ in range(20):
        await bus.publish(k, {"type": "text_delta", "text": "xxxxx"})
    gen = bus.stream(k)
    snap = await gen.__anext__()
    assert len(snap["blocks"][-1]["text"]) <= 11  # bounded (elision prefix allowed)
    await bus.close(k)
    await gen.aclose()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_run_event_bus.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'atom.workflow.events'`.

- [ ] **Step 3: Implement `src/atom/workflow/events.py`**

```python
"""In-memory async pub/sub for live run events (single-process; see the streaming design doc).

One channel per (run_id, step, task). A channel keeps subscriber queues plus a bounded, coalesced
accumulator so a late subscriber (a page refresh) gets a catch-up snapshot then live deltas. Closed
channels are retained (LRU-bounded by ``retain_closed``) so a subscriber joining just after a task
completes still catches up before falling back to the durable on-disk chat. Transport-agnostic: it
moves opaque event dicts between one producer (the task) and N consumers (SSE handlers).
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import AsyncIterator, Optional


def channel_key(run_id: str, step: int, task_id: str) -> str:
    return f"{run_id}:s{step}:{task_id}"


_TERMINAL = object()  # sentinel pushed to subscriber queues on close


class _Channel:
    def __init__(self, max_chars: int, queue_max: int):
        self.max_chars = max_chars
        self.queue_max = queue_max
        self.subscribers: set[asyncio.Queue] = set()
        self.blocks: list[dict] = []          # coalesced accumulator (ordered atom events)
        self.closed = False
        self.error: Optional[str] = None

    def accumulate(self, event: dict) -> None:
        t = event.get("type")
        if t in ("text_delta", "thinking_delta") and self.blocks and self.blocks[-1].get("type") == t:
            merged = self.blocks[-1]["text"] + event.get("text", "")
            if len(merged) > self.max_chars:  # elide the oldest chars of this block to stay bounded
                merged = "…" + merged[-self.max_chars:]
            self.blocks[-1]["text"] = merged
        else:
            self.blocks.append(dict(event))


class RunEventBus:
    def __init__(self, *, max_chars: int = 20000, queue_max: int = 512,
                 retain_closed: int = 64, heartbeat_seconds: float = 15.0):
        self._chans: "OrderedDict[str, _Channel]" = OrderedDict()
        self.max_chars = max_chars
        self.queue_max = queue_max
        self.retain_closed = retain_closed
        self.heartbeat = heartbeat_seconds

    def _ensure(self, key: str) -> _Channel:
        ch = self._chans.get(key)
        if ch is None:
            ch = _Channel(self.max_chars, self.queue_max)
            self._chans[key] = ch
        self._chans.move_to_end(key)
        return ch

    async def publish(self, key: str, event: dict) -> None:
        ch = self._ensure(key)
        if ch.closed:
            return
        ch.accumulate(event)
        for q in list(ch.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # drop; the client re-syncs from a fresh snapshot on reconnect

    async def close(self, key: str, error: Optional[str] = None) -> None:
        ch = self._ensure(key)
        ch.closed = True
        ch.error = error
        for q in list(ch.subscribers):
            try:
                q.put_nowait(_TERMINAL)
            except asyncio.QueueFull:
                pass
        self._evict_closed()

    def _evict_closed(self) -> None:
        # Bound retained completed channels (oldest first, only those with no live subscribers).
        while sum(1 for c in self._chans.values() if c.closed) > self.retain_closed:
            for k, c in list(self._chans.items()):
                if c.closed and not c.subscribers:
                    self._chans.pop(k, None)
                    break
            else:
                break

    async def stream(self, key: str) -> AsyncIterator[dict]:
        ch = self._ensure(key)
        q: asyncio.Queue = asyncio.Queue(maxsize=self.queue_max)
        ch.subscribers.add(q)
        try:
            yield {"type": "snapshot", "blocks": [dict(b) for b in ch.blocks]}
            if ch.closed:
                yield {"type": "done", "error": ch.error}
                return
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=self.heartbeat)
                except asyncio.TimeoutError:
                    yield {"type": "ping"}
                    continue
                if ev is _TERMINAL:
                    yield {"type": "done", "error": ch.error}
                    return
                yield ev
        finally:
            ch.subscribers.discard(q)
            self._evict_closed()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_run_event_bus.py -v`
Expected: PASS (all 5).

- [ ] **Step 5: Commit**

```bash
git add src/atom/workflow/events.py tests/test_run_event_bus.py
git commit -m "feat(workflow): in-memory RunEventBus for live run events"
```

---

### Task 3: Event translation + coalescing emitter

**Files:**
- Create: `src/atom/streaming.py`
- Test: `tests/test_streaming.py`

**Interfaces:**
- Consumes: `atom.messages.message_text` (existing).
- Produces:
  - constants `THINKING="thinking_delta"`, `TEXT="text_delta"`, `TOOL_CALL="tool_call"`, `TOOL_RESULT="tool_result"`
  - `translate_message_chunk(chunk, metadata: dict | None) -> list[dict]` — emits `text_delta`/`thinking_delta`; returns `[]` for sub-agent chunks.
  - `translate_update(messages: list) -> list[dict]` — emits `tool_call` (from `AIMessage.tool_calls`) and `tool_result` (from `ToolMessage`).
  - `class StreamEmitter(publish: Callable[[dict], Awaitable[None]], *, coalesce_ms: int, coalesce_chars: int)` with `async def emit(self, event: dict)` and `async def aclose(self)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_streaming.py`:

```python
"""Chunk->atom-event translation + coalescing emitter."""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from atom.streaming import (
    StreamEmitter, translate_message_chunk, translate_update,
)


class _Chunk:
    """Minimal stand-in exposing content_blocks like an AIMessageChunk."""
    def __init__(self, blocks):
        self.content_blocks = blocks
        self.text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def test_translate_text_and_thinking_blocks():
    chunk = _Chunk([
        {"type": "reasoning", "reasoning": "let me think"},
        {"type": "text", "text": "hello"},
    ])
    out = translate_message_chunk(chunk, {})
    assert out == [
        {"type": "thinking_delta", "text": "let me think"},
        {"type": "text_delta", "text": "hello"},
    ]


def test_translate_filters_subagent_chunks():
    chunk = _Chunk([{"type": "text", "text": "child output"}])
    assert translate_message_chunk(chunk, {"atom_subagent": True}) == []
    assert translate_message_chunk(chunk, {"metadata": {"atom_subagent": True}}) == []


def test_translate_update_tool_call_and_result():
    ai = AIMessage(content="", tool_calls=[{"name": "bash", "args": {"cmd": "ls"}, "id": "c1", "type": "tool_call"}])
    tm = ToolMessage(content="file.txt", name="bash", tool_call_id="c1")
    assert translate_update([ai]) == [{"type": "tool_call", "id": "c1", "name": "bash", "args": {"cmd": "ls"}}]
    out = translate_update([tm])
    assert out[0]["type"] == "tool_result" and out[0]["name"] == "bash" and out[0]["text"] == "file.txt"
    assert out[0]["is_error"] is False


@pytest.mark.asyncio
async def test_emitter_coalesces_text_then_flushes_on_structural_event():
    sink = []
    async def pub(e): sink.append(e)
    em = StreamEmitter(pub, coalesce_ms=100000, coalesce_chars=100000)  # never auto-flush on size/time
    await em.emit({"type": "text_delta", "text": "he"})
    await em.emit({"type": "text_delta", "text": "llo"})
    assert sink == []                                   # buffered, not yet flushed
    await em.emit({"type": "tool_call", "name": "bash", "args": {}})
    assert sink == [{"type": "text_delta", "text": "hello"}, {"type": "tool_call", "name": "bash", "args": {}}]


@pytest.mark.asyncio
async def test_emitter_flushes_thinking_before_text():
    sink = []
    async def pub(e): sink.append(e)
    em = StreamEmitter(pub, coalesce_ms=100000, coalesce_chars=100000)
    await em.emit({"type": "thinking_delta", "text": "think"})
    await em.emit({"type": "text_delta", "text": "answer"})
    await em.aclose()
    assert sink == [{"type": "thinking_delta", "text": "think"}, {"type": "text_delta", "text": "answer"}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_streaming.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'atom.streaming'`.

- [ ] **Step 3: Implement `src/atom/streaming.py`**

```python
"""Translate a LangChain agent stream into atom's provider-agnostic event dicts, and coalesce
text/thinking deltas before publishing. Pure of transport (no bus / HTTP dependency).

Event dicts (the wire contract consumed by RunEventBus and the SSE endpoint):
  {"type": "thinking_delta", "text": str}
  {"type": "text_delta",     "text": str}
  {"type": "tool_call",      "id": str|None, "name": str|None, "args": dict}
  {"type": "tool_result",    "tool_call_id": str|None, "name": str|None, "text": str, "is_error": bool}
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from langchain_core.messages import AIMessage, ToolMessage

from atom.messages import message_text

THINKING = "thinking_delta"
TEXT = "text_delta"
TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"


def _is_subagent(metadata: dict | None) -> bool:
    md = metadata or {}
    if md.get("atom_subagent"):
        return True
    inner = md.get("metadata")
    return bool(isinstance(inner, dict) and inner.get("atom_subagent"))


def translate_message_chunk(chunk: Any, metadata: dict | None) -> list[dict]:
    """Emit thinking/text deltas from a streamed message chunk. Sub-agent chunks are dropped."""
    if _is_subagent(metadata):
        return []
    out: list[dict] = []
    blocks = getattr(chunk, "content_blocks", None)
    if not blocks:
        text = getattr(chunk, "text", "") or ""
        if text:
            out.append({"type": TEXT, "text": text})
        return out
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t in ("reasoning", "thinking"):
            txt = b.get("reasoning") or b.get("thinking") or b.get("text") or ""
            if txt:
                out.append({"type": THINKING, "text": txt})
        elif t == "text":
            txt = b.get("text") or ""
            if txt:
                out.append({"type": TEXT, "text": txt})
    return out


def translate_update(messages: list) -> list[dict]:
    """Emit tool_call events (from a completed AIMessage) and tool_result events (from a ToolMessage).
    Never emits assistant text — that arrives token-by-token via translate_message_chunk (no dup)."""
    out: list[dict] = []
    for m in messages or []:
        if isinstance(m, AIMessage):
            for tc in (m.tool_calls or []):
                out.append({"type": TOOL_CALL, "id": tc.get("id"),
                            "name": tc.get("name"), "args": tc.get("args", {})})
        elif isinstance(m, ToolMessage):
            out.append({"type": TOOL_RESULT,
                        "tool_call_id": getattr(m, "tool_call_id", None),
                        "name": getattr(m, "name", None),
                        "text": message_text(m),
                        "is_error": getattr(m, "status", None) == "error"})
    return out


class StreamEmitter:
    """Coalesces consecutive text/thinking deltas (by ms window OR char count) before publishing,
    and flushes pending text before any structural event and on close. Event-driven (no timer task):
    the time check fires on each incoming delta, which is exactly when a high-rate stream needs it."""

    def __init__(self, publish: Callable[[dict], Awaitable[None]], *,
                 coalesce_ms: int, coalesce_chars: int):
        self._publish = publish
        self._coalesce_s = max(0, coalesce_ms) / 1000.0
        self._coalesce_chars = max(1, coalesce_chars)
        self._buf_type: str | None = None
        self._buf_text: str = ""
        self._last_flush: float = 0.0
        self._started = False

    def _now(self) -> float:
        return asyncio.get_running_loop().time()

    async def emit(self, event: dict) -> None:
        t = event.get("type")
        if t in (TEXT, THINKING):
            if not self._started:
                self._last_flush = self._now()
                self._started = True
            if self._buf_type and self._buf_type != t:
                await self._flush()
            self._buf_type = t
            self._buf_text += event.get("text", "")
            if (len(self._buf_text) >= self._coalesce_chars
                    or (self._now() - self._last_flush) >= self._coalesce_s):
                await self._flush()
        else:
            await self._flush()
            await self._publish(event)

    async def _flush(self) -> None:
        if self._buf_text:
            await self._publish({"type": self._buf_type, "text": self._buf_text})
            self._buf_text = ""
            self._last_flush = self._now()

    async def aclose(self) -> None:
        await self._flush()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_streaming.py -v`
Expected: PASS (all 5).

- [ ] **Step 5: Commit**

```bash
git add src/atom/streaming.py tests/test_streaming.py
git commit -m "feat(streaming): chunk->atom-event translation + coalescing emitter"
```

---

### Task 4: Stream in `run_agent`

**Files:**
- Modify: `src/atom/runtime.py` (`run_agent` signature + the `ainvoke` call at `runtime.py:126`)
- Modify: `tests/conftest.py` (add a text-streaming fake model + `make_streaming_prepared`)
- Test: `tests/test_runtime_streaming.py`

**Interfaces:**
- Consumes: `atom.streaming.translate_message_chunk`, `atom.streaming.translate_update`.
- Produces: `run_agent(..., on_event: Callable[[dict], Awaitable[None]] | None = None) -> RunResult` — when `on_event` is set and `cfg.streaming.enabled`, drives the agent via `astream` and awaits each translated event; otherwise unchanged. `RunResult` identical either way.

- [ ] **Step 1: Add a text-streaming fake to `tests/conftest.py`**

Append to `tests/conftest.py`:

```python
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk


class StreamingTextChatModel(BaseChatModel):
    """Streams a fixed text word-by-word via _astream so astream(stream_mode='messages') yields
    multiple text deltas. Falls back to _agenerate for the ainvoke path."""
    text: str = "hello streamed world"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.text))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        return self._generate(messages)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        for word in self.text.split(" "):
            piece = word + " "
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=piece))
            if run_manager:
                await run_manager.on_llm_new_token(piece, chunk=chunk)
            yield chunk

    @property
    def _llm_type(self) -> str:
        return "streaming-text"


def make_streaming_prepared(text: str = "hello streamed world") -> PreparedModel:
    model = StreamingTextChatModel(text=text, profile=DEFAULT_PROFILE_DATA)
    caps = {
        "context_window": model.profile["max_input_tokens"],
        "max_output_tokens": model.profile["max_output_tokens"],
        "supports_vision": model.profile["image_inputs"],
        "supports_reasoning": model.profile["reasoning_output"],
        "has_profile": True,
    }
    return PreparedModel(model=model, caps=caps, context_window=caps["context_window"])
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_runtime_streaming.py`:

```python
"""run_agent streaming path: emits events, preserves the RunResult contract, no-op when disabled."""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from atom.runtime import run_agent
from tests.conftest import make_prepared, make_streaming_prepared


@pytest.mark.asyncio
async def test_streaming_emits_text_deltas_and_preserves_result(base_config):
    events = []
    async def on_event(e): events.append(e)
    prepared = make_streaming_prepared("alpha beta gamma")
    result = await run_agent("hi", config=base_config, prepared=prepared, on_event=on_event)
    # contract preserved
    assert result.final_text.strip() == "alpha beta gamma"
    assert result.awaiting_clarification is False
    # some text streamed before the end
    assert any(e["type"] == "text_delta" for e in events)
    assert "".join(e.get("text", "") for e in events if e["type"] == "text_delta").strip() == "alpha beta gamma"


@pytest.mark.asyncio
async def test_streaming_emits_tool_events(base_config, atom_home):
    events = []
    async def on_event(e): events.append(e)
    ws = "/mnt/user-data/workspace"
    prepared = make_prepared([
        AIMessage(content="", tool_calls=[{"name": "write_file",
            "args": {"description": "w", "path": f"{ws}/o.txt", "content": "x\n"}, "id": "c1", "type": "tool_call"}]),
        AIMessage(content="done"),
    ])
    result = await run_agent("hi", config=base_config, prepared=prepared,
                             workspace=str(atom_home), on_event=on_event)
    assert result.final_text == "done"
    kinds = [e["type"] for e in events]
    assert "tool_call" in kinds and "tool_result" in kinds


@pytest.mark.asyncio
async def test_no_on_event_is_unchanged(base_config):
    prepared = make_prepared([AIMessage(content="plain")])
    result = await run_agent("hi", config=base_config, prepared=prepared)  # on_event=None
    assert result.final_text == "plain"


@pytest.mark.asyncio
async def test_streaming_disabled_config_skips_stream(base_config):
    base_config.streaming.enabled = False
    events = []
    async def on_event(e): events.append(e)
    prepared = make_prepared([AIMessage(content="plain")])
    result = await run_agent("hi", config=base_config, prepared=prepared, on_event=on_event)
    assert result.final_text == "plain"
    assert events == []  # streaming disabled -> ainvoke path, no events
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_runtime_streaming.py -v`
Expected: FAIL with `TypeError: run_agent() got an unexpected keyword argument 'on_event'`.

- [ ] **Step 4: Modify `run_agent`**

In `src/atom/runtime.py`, add the parameter to the signature (after `notes: dict | None = None`):

```python
    notes: dict | None = None,
    on_event: "Callable[[dict], Awaitable[None]] | None" = None,
) -> RunResult:
```

(No new import is required: `runtime.py` has `from __future__ import annotations`, so the quoted annotation is a forward-ref string evaluated never — `Callable`/`Awaitable` don't need importing. The callback is only ever *called* (`await on_event(ev)`), not isinstance-checked.)

Replace the `ainvoke` call block (`runtime.py:126-130`):

```python
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=content)]},
            config=run_config,
            context=context,
        )
```

with the streaming-or-buffered drive:

```python
        inp = {"messages": [HumanMessage(content=content)]}
        if on_event is not None and cfg.streaming.enabled:
            from atom.streaming import translate_message_chunk, translate_update

            async for item in agent.astream(
                inp, config=run_config, context=context, stream_mode=["messages", "updates"],
            ):
                # Compiled-graph astream yields (mode, data) tuples; the create_agent sugar yields
                # {"type","data"} dicts. Normalize both so the translator sees one shape.
                mode, data = item if isinstance(item, tuple) else (item.get("type"), item.get("data"))
                if mode == "messages":
                    chunk, metadata = data
                    for ev in translate_message_chunk(chunk, metadata):
                        await on_event(ev)
                elif mode == "updates":
                    for _node, update in (data or {}).items():
                        msgs = update.get("messages") if isinstance(update, dict) else None
                        for ev in translate_update(msgs or []):
                            await on_event(ev)
            # aget_state gives the authoritative final channel values (messages + artifacts + title),
            # equivalent to what ainvoke returned — the checkpointer is still open in this context.
            result = (await agent.aget_state(run_config)).values
        else:
            result = await agent.ainvoke(inp, config=run_config, context=context)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_runtime_streaming.py tests/test_runtime_trace.py -v`
Expected: PASS. (If `test_streaming_emits_text_deltas_and_preserves_result` shows the text as a single delta rather than word-by-word, that is acceptable — the assertion checks the concatenation, not chunk count.)

- [ ] **Step 6: Run the broader suite to confirm no regressions**

Run: `.venv/bin/python -m pytest tests/test_runtime_streaming.py tests/test_agent_smoke.py tests/test_clarification.py -v`
Expected: PASS (the `ainvoke` path is untouched for existing callers).

- [ ] **Step 7: Commit**

```bash
git add src/atom/runtime.py tests/conftest.py tests/test_runtime_streaming.py
git commit -m "feat(runtime): stream LLM output via astream with on_event callback"
```

---

### Task 5: Wire the engine to the bus

**Files:**
- Modify: `src/atom/workflow/engine.py` (`WorkflowEngine.__init__`, `_run_task` at `engine.py:366-433`)
- Test: `tests/test_workflow_engine_streaming.py`

**Interfaces:**
- Consumes: `atom.streaming.StreamEmitter`, `atom.workflow.events.RunEventBus`, `channel_key`.
- Produces: `WorkflowEngine.bus: RunEventBus`; `_run_task` publishes events to `engine.bus` for the task's channel and closes it in a `finally`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_workflow_engine_streaming.py`:

```python
"""Engine streaming: a task's events reach the bus and the run still completes normally."""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from atom.workflow.engine import WorkflowEngine
from atom.workflow.events import channel_key
from atom.workflow.schema import WorkflowDef
from tests.conftest import make_prepared

WS = "/mnt/user-data/workspace"


def _wf() -> WorkflowDef:
    return WorkflowDef.model_validate({
        "name": "demo", "inputs": [{"name": "topic", "required": True}],
        "steps": [{"title": "Draft", "tasks": [{"id": "t1", "prompt": "write {{ topic }}"}]}],
    })


def _provider(td, sd, wf):
    return make_prepared([
        AIMessage(content="", tool_calls=[{"name": "write_file",
            "args": {"description": "w", "path": f"{WS}/o.txt", "content": "hi\n"}, "id": "c1", "type": "tool_call"}]),
        AIMessage(content="all done"),
    ])


@pytest.mark.asyncio
async def test_task_publishes_events_and_completes(base_config, atom_home):
    # Run to completion, THEN read the retained closed channel — deterministic (no subscribe-timing
    # race). Live delivery is covered by the bus unit tests (Task 2) and the SSE test (Task 6).
    engine = WorkflowEngine(base_config, prepared_provider=_provider)
    key = channel_key("runS", 0, "t1")

    engine.create_run(_wf(), {"topic": "sea"}, "runS", "2026-07-16T00:00:00")
    manifest = await engine.execute("runS")
    assert manifest.status == "complete"

    seen = [ev async for ev in engine.bus.stream(key)]
    assert seen[0]["type"] == "snapshot"
    block_types = [b["type"] for b in seen[0]["blocks"]]
    assert "tool_call" in block_types and "tool_result" in block_types  # engine published task events
    assert seen[-1] == {"type": "done", "error": None}
    # durable snapshot still written (unchanged behavior)
    assert engine.store.load_chat("runS", 0, "t1") is not None


@pytest.mark.asyncio
async def test_streaming_disabled_still_runs(base_config, atom_home):
    base_config.streaming.enabled = False
    engine = WorkflowEngine(base_config, prepared_provider=_provider)
    engine.create_run(_wf(), {"topic": "sea"}, "runD", "2026-07-16T00:00:00")
    manifest = await engine.execute("runD")
    assert manifest.status == "complete"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_workflow_engine_streaming.py -v`
Expected: FAIL with `AttributeError: 'WorkflowEngine' object has no attribute 'bus'`.

- [ ] **Step 3: Add the bus to `WorkflowEngine.__init__`**

In `src/atom/workflow/engine.py`, add the import near the top imports:

```python
from atom.streaming import StreamEmitter
from atom.workflow.events import RunEventBus, channel_key
```

In `__init__`, after `self._task_cfg = self._build_task_cfg(cfg)` (~line 66):

```python
        self.bus = RunEventBus(
            max_chars=cfg.streaming.accumulator_max_chars,
            queue_max=cfg.streaming.subscriber_queue_max,
            retain_closed=cfg.streaming.retain_closed,
            heartbeat_seconds=cfg.streaming.heartbeat_seconds,
        )
```

- [ ] **Step 4: Wire `_run_task`**

In `_run_task` (`engine.py:366`), at the very top of the method body (before `timeout: Optional[float] = None`), add:

```python
        key = channel_key(manifest.run_id, step_state.index, ts.id)
        emitter: "StreamEmitter | None" = None
```

Inside the `try`, right before `coro = run_agent(` (~line 396), build the emitter and pass it:

```python
            if self.cfg.streaming.enabled:
                emitter = StreamEmitter(
                    lambda e, k=key: self.bus.publish(k, e),
                    coalesce_ms=self.cfg.streaming.coalesce_ms,
                    coalesce_chars=self.cfg.streaming.coalesce_chars,
                )
```

Add `on_event=` to the `run_agent(...)` call (add as a new kwarg alongside `notes=`):

```python
                notes=notes.as_prompt_ctx() if notes else None,
                on_event=(emitter.emit if emitter else None),
            )
```

Finally, at the END of `_run_task`, replace the trailing best-effort save block:

```python
        ts.ended_at = _now()
        try:
            self.store.save(manifest)
        except Exception:
            pass  # best-effort: this method must never raise
```

with a version that also closes the stream (still best-effort, still never raises):

```python
        ts.ended_at = _now()
        if emitter is not None:
            try:
                await emitter.aclose()
            except Exception:
                pass
            try:
                await self.bus.close(key, error=(ts.error if ts.status == "failed" else None))
            except Exception:
                pass
        try:
            self.store.save(manifest)
        except Exception:
            pass  # best-effort: this method must never raise
```

Note: the `except asyncio.CancelledError` branch `raise`s before reaching this trailing block, so a cancelled task's channel is closed by the engine-level cancellation path; the finally in `execute()` already terminalizes the run. Leaving the cancelled channel to be LRU-evicted is acceptable (subscribers get the snapshot then time out via heartbeat and the client falls back to polling).

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_engine_streaming.py tests/test_workflow_engine.py -v`
Expected: PASS (new streaming tests + all existing engine tests still green).

- [ ] **Step 6: Commit**

```bash
git add src/atom/workflow/engine.py tests/test_workflow_engine_streaming.py
git commit -m "feat(workflow): publish live task events to the RunEventBus"
```

---

### Task 6: SSE endpoint

**Files:**
- Modify: `src/atom/api/app.py` (add import; add route after the `messages` route ~line 198)
- Test: `tests/test_workflow_api_streaming.py`

**Interfaces:**
- Consumes: `engine.bus` (`RunEventBus`), `channel_key`, `cfg.streaming.enabled`.
- Produces: `GET /api/runs/{run_id}/tasks/{step}/{task_id}/stream` → `text/event-stream`; `404` when streaming disabled.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_workflow_api_streaming.py`:

```python
"""SSE stream endpoint: frame format, snapshot+done, 404 when disabled."""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from atom.api.app import create_app
from atom.workflow.engine import WorkflowEngine
from atom.workflow.events import channel_key


@asynccontextmanager
async def _client(app):
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


@pytest.mark.asyncio
async def test_stream_yields_snapshot_then_done(base_config, atom_home):
    engine = WorkflowEngine(base_config)
    app = create_app(base_config, engine=engine)
    key = channel_key("r1", 0, "t1")
    await engine.bus.publish(key, {"type": "text_delta", "text": "hello"})
    await engine.bus.close(key)

    async with _client(app) as c:
        async with c.stream("GET", "/api/runs/r1/tasks/0/t1/stream") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            body = ""
            async for chunk in resp.aiter_text():
                body += chunk
    assert "event: snapshot" in body
    assert "hello" in body
    assert "event: done" in body


@pytest.mark.asyncio
async def test_stream_404_when_disabled(base_config, atom_home):
    base_config.streaming.enabled = False
    engine = WorkflowEngine(base_config)
    app = create_app(base_config, engine=engine)
    async with _client(app) as c:
        resp = await c.get("/api/runs/r1/tasks/0/t1/stream")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workflow_api_streaming.py -v`
Expected: FAIL (route returns 404/not found for both, or 200 mismatch).

- [ ] **Step 3: Add the endpoint to `src/atom/api/app.py`**

Add to the imports at the top (with the other `fastapi.responses` import at line 17):

```python
from fastapi.responses import FileResponse, StreamingResponse
```

Add the import for the key helper (near the other workflow imports, ~line 23):

```python
from atom.workflow.events import channel_key
```

Add the route after the `get_messages` route (~line 198):

```python
    @app.get("/api/runs/{run_id}/tasks/{step}/{task_id}/stream")
    async def stream_task(run_id: str, step: int, task_id: str):
        """Server-Sent Events: live thinking/text/tool deltas for one task. Emits a `snapshot`
        (catch-up) then live frames + `ping` heartbeats, ending with `done` (or `error`) on task
        completion — at which point the client refetches the authoritative .../messages snapshot."""
        if not cfg.streaming.enabled:
            raise HTTPException(404, "streaming disabled")
        key = channel_key(run_id, step, task_id)

        async def gen():
            async for ev in engine.bus.stream(key):
                if ev.get("type") == "ping":
                    yield ": ping\n\n"                       # SSE comment (keep-alive)
                    continue
                yield f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"

        return StreamingResponse(
            gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_workflow_api_streaming.py tests/test_workflow_api.py -v`
Expected: PASS (new streaming tests + all existing API tests).

- [ ] **Step 5: Commit**

```bash
git add src/atom/api/app.py tests/test_workflow_api_streaming.py
git commit -m "feat(api): SSE endpoint for live task streaming"
```

---

### Task 7: Filter sub-agent deltas at the source

**Files:**
- Modify: `src/atom/subagent.py` (`_child_config`, ~line 72)
- Test: `tests/test_subagent.py` (add one assertion)

**Interfaces:**
- Produces: `SubagentRunner._child_config(child_id)` now includes `metadata={"atom_subagent": True}`, which `translate_message_chunk` (Task 3) filters on so a delegated sub-agent's token deltas never leak into the lead's live stream.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_subagent.py`:

```python
def test_child_config_tags_subagent():
    from atom.subagent import SubagentRunner
    # Construct minimally; _child_config only reads self.recursion_limit.
    runner = SubagentRunner.__new__(SubagentRunner)
    runner.recursion_limit = 300
    cfg = runner._child_config("child-1")
    assert cfg["metadata"] == {"atom_subagent": True}
    assert cfg["configurable"]["thread_id"] == "child-1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_subagent.py::test_child_config_tags_subagent -v`
Expected: FAIL with `KeyError: 'metadata'`.

- [ ] **Step 3: Add the marker**

In `src/atom/subagent.py`, change `_child_config` (~line 72):

```python
    def _child_config(self, child_id: str) -> dict:
        return {
            "configurable": {"thread_id": child_id},
            "recursion_limit": self.recursion_limit,
            # Tag child runs so their streamed model deltas are filtered out of the lead's live
            # stream (see atom.streaming.translate_message_chunk). Metadata is key-overridden per
            # run in LangSmith, so this stays isolated to the child and never leaks upward.
            "metadata": {"atom_subagent": True},
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_subagent.py -v`
Expected: PASS (new assertion + existing sub-agent tests unaffected).

- [ ] **Step 5: Commit**

```bash
git add src/atom/subagent.py tests/test_subagent.py
git commit -m "feat(subagent): tag child runs so their deltas are filtered from the lead stream"
```

---

### Task 8: Frontend live streaming

**Files:**
- Modify: `atom-ui/src/api.ts` (add `streamUrl` + block types)
- Modify: `atom-ui/src/RunView.tsx` (add `useTaskStream`; render live blocks in `Transcript`)
- Modify: `atom-ui/src/styles.css` (caret + thinking styles)

**Interfaces:**
- Consumes: the SSE endpoint from Task 6; `api.messages` (existing) for the authoritative post-completion transcript.
- Produces: a live transcript for the selected running task; falls back to the fetched snapshot when not streaming.

- [ ] **Step 1: Add the stream URL + types to `atom-ui/src/api.ts`**

Add these exported types near the other interfaces (after `ChatMsg`, ~line 16):

```ts
export type StreamBlock =
  | { kind: "thinking"; text: string }
  | { kind: "text"; text: string }
  | { kind: "tool_call"; id?: string; name?: string; args?: Record<string, unknown> }
  | { kind: "tool_result"; name?: string; text: string; isError: boolean };
```

Add to the `api` object (after `messages`, ~line 57):

```ts
  streamUrl: (id: string, step: number, task: string): string =>
    `/api/runs/${id}/tasks/${step}/${encodeURIComponent(task)}/stream`,
```

- [ ] **Step 2: Add the `useTaskStream` hook to `atom-ui/src/RunView.tsx`**

Add imports at the top (extend the existing api import and React import):

```ts
import { useEffect, useRef, useState } from "react";
import { api, artifactUrl, Artifact, ChatMsg, Manifest, StreamBlock } from "./api";
```

Add this hook near the bottom of the file (before `function DownloadCard`):

```ts
// Opens ONE EventSource for a running task and folds SSE events into an ordered block list.
// Closes on done/error (or when the task is no longer running); the caller then refetches the
// authoritative persisted transcript. Switching task tears down the old connection.
function useTaskStream(runId: string, sel: Sel | null, taskStatus: string | undefined) {
  const [blocks, setBlocks] = useState<StreamBlock[]>([]);
  const [streaming, setStreaming] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    esRef.current?.close();
    esRef.current = null;
    setBlocks([]);
    setStreaming(false);
    if (!sel || taskStatus !== "running") return;

    const es = new EventSource(api.streamUrl(runId, sel.step, sel.task));
    esRef.current = es;
    setStreaming(true);

    const appendText = (kind: "thinking" | "text", text: string) =>
      setBlocks((prev) => {
        const last = prev[prev.length - 1];
        if (last && last.kind === kind) {
          const next = prev.slice(0, -1);
          return [...next, { ...last, text: last.text + text }];
        }
        return [...prev, { kind, text } as StreamBlock];
      });

    es.addEventListener("snapshot", (e) => {
      const { blocks: bs } = JSON.parse((e as MessageEvent).data);
      // Map accumulator events (typed by wire name) into render blocks.
      const mapped: StreamBlock[] = (bs || []).map((b: any) =>
        b.type === "thinking_delta" ? { kind: "thinking", text: b.text }
        : b.type === "text_delta" ? { kind: "text", text: b.text }
        : b.type === "tool_call" ? { kind: "tool_call", id: b.id, name: b.name, args: b.args }
        : { kind: "tool_result", name: b.name, text: b.text, isError: b.is_error });
      setBlocks(mapped);
    });
    es.addEventListener("thinking_delta", (e) => appendText("thinking", JSON.parse((e as MessageEvent).data).text));
    es.addEventListener("text_delta", (e) => appendText("text", JSON.parse((e as MessageEvent).data).text));
    es.addEventListener("tool_call", (e) => {
      const d = JSON.parse((e as MessageEvent).data);
      setBlocks((prev) => [...prev, { kind: "tool_call", id: d.id, name: d.name, args: d.args }]);
    });
    es.addEventListener("tool_result", (e) => {
      const d = JSON.parse((e as MessageEvent).data);
      setBlocks((prev) => [...prev, { kind: "tool_result", name: d.name, text: d.text, isError: d.is_error }]);
    });
    const end = () => { setStreaming(false); es.close(); if (esRef.current === es) esRef.current = null; };
    es.addEventListener("done", end);   // terminal frame (carries an `error` field on task failure)
    // Native EventSource "error" = transient connection drop; leave it to auto-reconnect. Final
    // teardown also happens when the task leaves "running" (the effect deps re-run and close es).

    return () => { es.close(); if (esRef.current === es) esRef.current = null; };
  }, [runId, sel?.step, sel?.task, taskStatus]);

  return { blocks, streaming };
}
```

- [ ] **Step 3: Render live blocks in `Transcript`**

Replace the `Transcript` function body so it prefers the live stream while the task runs, and the fetched snapshot otherwise. Update its call site in `RunView` to pass the selected task's status.

At the `Transcript` call site (~line 214), pass the task status:

```tsx
            {tab === "transcript"
              ? <Transcript runId={runId} sel={sel} status={manifest.status} taskStatus={selTask?.status} />
              : <Deliverables runId={runId} arts={arts} open={openArt} setOpen={setOpenArt} />}
```

(Note: `selTask` is already computed at `RunView.tsx:107-109`.)

Replace the `Transcript` component (`RunView.tsx:223-261`) with:

```tsx
function Transcript(
  { runId, sel, status, taskStatus }:
  { runId: string; sel: Sel | null; status: string; taskStatus?: string },
) {
  const [chat, setChat] = useState<ChatMsg[]>([]);
  const [pending, setPending] = useState(false);
  const { blocks, streaming } = useTaskStream(runId, sel, taskStatus);

  useEffect(() => {
    if (!sel) { setChat([]); return; }
    let live = true;
    setPending(true);
    api.messages(runId, sel.step, sel.task)
      .then((m) => { if (live) setChat(m); })
      .catch(() => { if (live) setChat([]); })
      .finally(() => { if (live) setPending(false); });
    return () => { live = false; };
  }, [runId, sel?.step, sel?.task, status, taskStatus]);

  if (!sel) return <div className="placeholder">Select a task to view its transcript.</div>;

  // Live stream takes over while the task runs and has produced something.
  if (streaming && blocks.length) {
    return (
      <div className="transcript">
        {blocks.map((b, i) => {
          const isLast = i === blocks.length - 1;
          if (b.kind === "thinking")
            return <div key={i} className="msg thinking"><div className="msg-role">thinking</div>
              <div className="msg-text think">{b.text}{isLast && <span className="caret" />}</div></div>;
          if (b.kind === "text")
            return <div key={i} className="msg ai"><div className="msg-role">assistant</div>
              <div className="msg-text">{b.text}{isLast && <span className="caret" />}</div></div>;
          if (b.kind === "tool_call")
            return <div key={i} className="msg tool-calls">
              <div className={`toolcall${b.name === "present_files" ? " present" : ""}`}>
                <span className="tc-name">→ {b.name}</span>
                <span className="tc-args">{argSummary(b.args)}</span></div></div>;
          return <div key={i} className={`msg tool${b.isError ? " err" : ""}`}>
            <div className="msg-role">{b.name || "tool"}</div>
            <div className="msg-text">{b.text}</div></div>;
        })}
      </div>
    );
  }

  if (pending && !chat.length) return <div className="placeholder">Loading transcript…</div>;
  if (!chat.length) return <div className="placeholder">No messages yet for {sel.task}.</div>;

  return (
    <div className="transcript">
      {chat.map((m, i) => m.tool_calls?.length ? (
        <div key={i} className="msg tool-calls">
          {m.text && <div className="msg-text">{m.text}</div>}
          {m.tool_calls.map((c, k) => (
            <div key={k} className={`toolcall${c.name === "present_files" ? " present" : ""}`}>
              <span className="tc-name">{c.name === "present_files" ? "⇪ present_files" : `→ ${c.name}`}</span>
              <span className="tc-args">{argSummary(c.args)}</span>
            </div>
          ))}
        </div>
      ) : (
        <div key={i} className={`msg ${m.role}`}>
          <div className="msg-role">{m.name || m.role}</div>
          <div className="msg-text">{m.text}</div>
        </div>
      ))}
    </div>
  );
}
```

- [ ] **Step 4: Add styles to `atom-ui/src/styles.css`**

Append (after the `.msg.tool .msg-text` rule ~line 162):

```css
.msg-text.think { color: var(--ink-2); font-style: italic; background: var(--surface-2, #f6f6f7); }
.caret { display: inline-block; width: 7px; height: 1.05em; margin-left: 2px; vertical-align: text-bottom;
  background: var(--accent); animation: blink 1s steps(2, start) infinite; }
@keyframes blink { to { visibility: hidden; } }
```

- [ ] **Step 5: Typecheck + build**

Run: `cd atom-ui && npm run build`
Expected: PASS (tsc typecheck + vite build succeed, no type errors).

- [ ] **Step 6: Manual verification against a real model**

This is the end-to-end check the automated tests can't fully cover (fakes don't stream token-by-token like Anthropic does). With an `ANTHROPIC_API_KEY` in `.env`:

Run: `.venv/bin/python -m atom serve` (in one terminal), open the UI, submit a real workflow run (e.g. `workflows/parallel-poems.yaml`), open the run, and confirm: thinking/assistant text appears incrementally with a blinking caret, tool calls/results appear as they happen, and on completion the transcript reconciles to the final persisted messages. Then set `streaming.enabled: false` in `config.yaml`, restart, and confirm the UI still works via polling (transcript appears at task end).

- [ ] **Step 7: Commit**

```bash
git add atom-ui/src/api.ts atom-ui/src/RunView.tsx atom-ui/src/styles.css
git commit -m "feat(ui): live-stream task thinking/text/tools via SSE"
```

---

## Final verification

- [ ] Run the full suite: `.venv/bin/python -m pytest -q` → all green.
- [ ] `cd atom-ui && npm run build` → clean.
- [ ] Manual: streaming on (live incremental transcript) and off (polling fallback) both work, per Task 8 Step 6.

## Self-Review notes (for the implementer)

- **Spec coverage:** RunEventBus (§1 → Task 2), run_agent streaming + final-state recovery + sub-agent filter (§2 → Tasks 4/7), engine wiring (§3 → Task 5), SSE endpoint (§4 → Task 6), frontend (§5 → Task 8), config block (§6 → Task 1). All spec sections map to a task.
- **Type consistency:** wire event dicts use `text_delta`/`thinking_delta`/`tool_call`/`tool_result` everywhere (bus accumulator, translator, SSE frames, frontend snapshot mapping). Frontend render blocks use `kind: thinking|text|tool_call|tool_result` (the UI-local shape mapped from the wire names in `useTaskStream`). `channel_key` has one definition (`events.py`) imported by engine + api.
- **Known acceptable gap:** token-granular text streaming is verified manually (Task 8 Step 6) rather than by a fake, because fake models don't reproduce Anthropic's token stream; the translator + transport + tool-event path are fully unit/integration tested.
