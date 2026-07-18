# TodoContinuationMiddleware Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep atom's lead agent on-track to finish its todos by nudging it back to work when it stops with incomplete todos, and by resetting the todo list each turn so a nudge never mis-fires on a stale plan.

**Architecture:** One new lead-only middleware (`TodoContinuationMiddleware`) with a `before_agent` per-turn reset and a bounded, progress-aware `after_model` continuation nudge (`can_jump_to=["model"]`). Wired into `_build_middlewares` right after the stock `TodoListMiddleware`, gated on a new `TodosConfig`. The nudge is bounded by a counter because `LoopDetectionMiddleware` only catches repeated *tool-call* loops, not no-tool-call nudge loops.

**Tech Stack:** Python 3.12, LangChain v1 `AgentMiddleware`, pydantic config (`_Base`), pytest + pytest-asyncio.

## Global Constraints

- Lead agent only. Do NOT add this middleware to the subagent chain (`src/atom/subagent.py`).
- Placement is load-bearing: register `TodoContinuationMiddleware` **immediately after** `TodoListMiddleware()` in `_build_middlewares` (early in the chain) so its `after_model` runs *after* `LoopDetectionMiddleware` and `ClarificationMiddleware` on the reverse unwind.
- Sync-only hooks (`before_agent`, `after_model`), matching `clarification.py` / `loop_detection.py` / `instruction_pin.py`. atom runs async via `astream`; sync hooks are invoked correctly.
- Config defaults: `continuation_nudge: bool = True`, `max_nudges: int = 2`. Existing configs must load unchanged.
- Injected nudge message: `HumanMessage` with `additional_kwargs={"lc_source": "todo_continuation"}`.
- Follow existing patterns: pydantic `_Base` sub-config with `Field(default_factory=...)`; middleware unit tests call hooks directly with a dict state and `runtime=None` (see `tests/test_middleware.py`).

---

### Task 1: Config — `TodosConfig`

**Files:**
- Modify: `src/atom/config/schema.py` (add `TodosConfig` after `GuardrailConfig` ~line 113; add `todos` field to `AtomConfig` ~line 189)
- Test: `tests/test_config.py` (add one test; create the file only if it does not already exist)

**Interfaces:**
- Produces: `atom.config.schema.TodosConfig` with fields `continuation_nudge: bool = True`, `max_nudges: int = 2`; `AtomConfig.todos: TodosConfig`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py` (create with the imports if missing):

```python
from atom.config.schema import AtomConfig, TodosConfig


def test_todos_config_defaults():
    cfg = AtomConfig()
    assert isinstance(cfg.todos, TodosConfig)
    assert cfg.todos.continuation_nudge is True
    assert cfg.todos.max_nudges == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py::test_todos_config_defaults -q`
Expected: FAIL — `ImportError: cannot import name 'TodosConfig'` (or `AttributeError: 'AtomConfig' object has no attribute 'todos'`).

- [ ] **Step 3: Write minimal implementation**

In `src/atom/config/schema.py`, add after `class GuardrailConfig(_Base):` block (~line 114):

```python
class TodosConfig(_Base):
    # When true, if the lead agent ends a turn with incomplete todos, nudge it to keep going
    # (up to max_nudges consecutive no-progress stalls) instead of stopping early.
    continuation_nudge: bool = True
    # Infinite-loop backstop: max consecutive no-progress nudges before the turn is allowed to end.
    max_nudges: int = 2
```

In `class AtomConfig(_Base):`, add alongside the other sub-config fields (after `guardrails:` ~line 188):

```python
    todos: TodosConfig = Field(default_factory=TodosConfig)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py::test_todos_config_defaults -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/atom/config/schema.py tests/test_config.py
git commit -m "feat(config): add TodosConfig (continuation_nudge, max_nudges)"
```

---

### Task 2: `TodoContinuationMiddleware`

**Files:**
- Create: `src/atom/middleware/todo_continuation.py`
- Test: `tests/test_todo_continuation.py`

**Interfaces:**
- Consumes: `langchain.agents.AgentState`; `langchain.agents.middleware.AgentMiddleware`, `hook_config`; `langchain_core.messages.AIMessage`, `HumanMessage`.
- Produces:
  - `TodoContinuationMiddleware(max_nudges: int = 2)` — `AgentMiddleware` with `state_schema = TodoNudgeState`.
  - `before_agent(state, runtime) -> dict` returns `{"todos": [], "todo_nudge": {"count": 0, "completed": 0}}`.
  - `after_model(state, runtime) -> dict | None` — decorated `@hook_config(can_jump_to=["model"])`. Returns `{"messages": [HumanMessage(...)], "jump_to": "model", "todo_nudge": {...}}` to continue, else `None`.
  - Module constant `_NUDGE_SOURCE = "todo_continuation"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_todo_continuation.py`:

```python
"""Unit tests for TodoContinuationMiddleware (per-turn reset + bounded continuation nudge)."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from atom.middleware.todo_continuation import TodoContinuationMiddleware, _NUDGE_SOURCE


def _todo(content, status):
    return {"content": content, "status": status}


def _mw(max_nudges=2):
    return TodoContinuationMiddleware(max_nudges=max_nudges)


def _nudge_msgs(out):
    return [m for m in out["messages"] if getattr(m, "additional_kwargs", {}).get("lc_source") == _NUDGE_SOURCE]


def test_before_agent_resets_todos_and_counter():
    out = _mw().before_agent({"todos": [_todo("old", "in_progress")]}, runtime=None)
    assert out == {"todos": [], "todo_nudge": {"count": 0, "completed": 0}}


def test_nudge_fires_when_incomplete():
    state = {
        "messages": [AIMessage(content="All set!")],
        "todos": [_todo("build", "in_progress")],
    }
    out = _mw().after_model(state, runtime=None)
    assert out is not None
    assert out["jump_to"] == "model"
    assert out["todo_nudge"] == {"count": 1, "completed": 0}
    nudges = _nudge_msgs(out)
    assert len(nudges) == 1
    assert "build" in nudges[0].content  # lists the open item


def test_no_nudge_when_all_completed():
    state = {
        "messages": [AIMessage(content="done")],
        "todos": [_todo("build", "completed"), _todo("ship", "completed")],
    }
    assert _mw().after_model(state, runtime=None) is None


def test_no_nudge_when_no_todos():
    state = {"messages": [AIMessage(content="42")], "todos": []}
    assert _mw().after_model(state, runtime=None) is None
    state2 = {"messages": [AIMessage(content="42")]}  # todos absent entirely
    assert _mw().after_model(state2, runtime=None) is None


def test_inert_when_last_message_has_tool_calls():
    ai = AIMessage(content="", tool_calls=[{"name": "write_todos", "args": {}, "id": "t1", "type": "tool_call"}])
    state = {"messages": [ai], "todos": [_todo("build", "in_progress")]}
    assert _mw().after_model(state, runtime=None) is None


def test_cap_stops_the_loop():
    # Already nudged max_nudges times with no progress since -> allow the turn to end.
    state = {
        "messages": [AIMessage(content="still working on it")],
        "todos": [_todo("build", "in_progress")],
        "todo_nudge": {"count": 2, "completed": 0},
    }
    assert _mw(max_nudges=2).after_model(state, runtime=None) is None


def test_progress_resets_the_budget():
    # count is at the cap, but one more todo is completed than at the last nudge -> nudge again at count 1.
    state = {
        "messages": [AIMessage(content="made progress")],
        "todos": [_todo("build", "completed"), _todo("ship", "in_progress")],
        "todo_nudge": {"count": 2, "completed": 0},
    }
    out = _mw(max_nudges=2).after_model(state, runtime=None)
    assert out is not None
    assert out["todo_nudge"] == {"count": 1, "completed": 1}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_todo_continuation.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'atom.middleware.todo_continuation'`.

- [ ] **Step 3: Write minimal implementation**

Create `src/atom/middleware/todo_continuation.py`:

```python
"""TodoContinuationMiddleware — keep the lead agent on-track to finish its todos.

Two mechanisms, lead-agent only:

* ``before_agent`` resets the ``todos`` channel to empty at the start of every turn, so a
  multi-turn thread never inherits a stale plan and the nudge below reasons only about the
  current turn's todos.
* ``after_model`` nudges the model back to work when it tries to end a turn (a no-tool-call
  ``AIMessage``) while todos are still incomplete. Bounded by a progress-aware counter, because
  LoopDetectionMiddleware only catches repeated *tool-call* signatures — a no-tool-call nudge
  loop would evade it, leaving only ``recursion_limit`` (a hard error) as a backstop.

Placement (load-bearing): registered right after ``TodoListMiddleware`` — EARLY in the chain —
so on the reverse ``after_model`` unwind it runs AFTER ClarificationMiddleware and
LoopDetectionMiddleware. When either of those jumps to ``end`` the graph short-circuits to the
exit node before this hook runs, so the nudge never fires on a clarification turn or a detected
loop. Writing ``{"todos": []}`` is legal because the channel is contributed by the always-on
TodoListMiddleware; the two are coupled by construction (both lead-only).
"""

from __future__ import annotations

from typing import Any, NotRequired

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage

_NUDGE_PREFIX = "[Automated planning check] "
_NUDGE_SOURCE = "todo_continuation"


class TodoNudgeState(AgentState):
    """State channel owned by TodoContinuationMiddleware (reset each turn by before_agent)."""

    # {"count": <consecutive no-progress nudges>, "completed": <completed-todo count at last nudge>}
    todo_nudge: NotRequired[dict]


def _incomplete(todos: list[dict]) -> list[dict]:
    return [t for t in todos if t.get("status") != "completed"]


def _nudge_text(incomplete: list[dict]) -> str:
    lines = "\n".join(
        f"- ({t.get('status', 'pending')}) {t.get('content', '')}" for t in incomplete
    )
    return (
        f"{_NUDGE_PREFIX}You ended your turn, but these todo items are still open:\n"
        f"{lines}\n"
        "If the task isn't finished, keep going — start the next step now. If it IS finished, "
        "call write_todos to mark these items completed (or remove ones no longer needed), then "
        "write your final answer. Don't stop with open todos unless you're blocked — if you are, "
        "say what's blocking you."
    )


class TodoContinuationMiddleware(AgentMiddleware):
    state_schema = TodoNudgeState

    def __init__(self, *, max_nudges: int = 2) -> None:
        super().__init__()
        self.max_nudges = max_nudges

    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any]:
        # New turn: drop any prior turn's plan + nudge bookkeeping so the nudge scopes to this turn.
        return {"todos": [], "todo_nudge": {"count": 0, "completed": 0}}

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages") or []
        if not messages:
            return None
        last = messages[-1]
        if not isinstance(last, AIMessage) or last.tool_calls:
            return None  # not a turn-ending (no-tool-call) assistant message
        todos = state.get("todos") or []
        if not todos:
            return None  # no plan -> trivial task, terminate normally
        incomplete = _incomplete(todos)
        if not incomplete:
            return None  # plan fully complete -> clean finish
        cur = state.get("todo_nudge") or {"count": 0, "completed": 0}
        completed_now = len(todos) - len(incomplete)
        made_progress = completed_now > cur.get("completed", 0)
        new_count = 1 if made_progress else cur.get("count", 0) + 1
        if new_count > self.max_nudges:
            return None  # budget exhausted for this no-progress streak -> let the turn end
        return {
            "messages": [
                HumanMessage(
                    content=_nudge_text(incomplete),
                    additional_kwargs={"lc_source": _NUDGE_SOURCE},
                )
            ],
            "jump_to": "model",
            "todo_nudge": {"count": new_count, "completed": completed_now},
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_todo_continuation.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/atom/middleware/todo_continuation.py tests/test_todo_continuation.py
git commit -m "feat(middleware): TodoContinuationMiddleware — bounded continuation nudge + per-turn reset"
```

---

### Task 3: Wire into the lead chain (gated on config) + ordering guards

**Files:**
- Modify: `src/atom/agent.py` (local imports in `_build_middlewares` ~line 218-238; chain assembly ~line 303-312)
- Test: `tests/test_todo_continuation.py` (append two chain tests)

**Interfaces:**
- Consumes: `atom.config.schema.TodosConfig` (Task 1); `TodoContinuationMiddleware` (Task 2); `atom.agent._build_middlewares` (existing).
- Produces: `TodoContinuationMiddleware` present in the built chain, before `LoopDetectionMiddleware` and `ClarificationMiddleware`, when `cfg.todos.continuation_nudge` is true; absent when false.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_todo_continuation.py`:

```python
def _build_chain(base_config, atom_home):
    from atom.agent import _build_middlewares
    from atom.library import load_library
    from atom.sandbox.provider import LocalSandboxProvider
    from tests.conftest import make_prepared

    prepared = make_prepared([])
    profile = base_config.profile("default")
    provider = LocalSandboxProvider()
    library = load_library(str(atom_home))
    return _build_middlewares(
        base_config, profile, prepared, provider, str(atom_home), prepared.model, library
    )


def test_nudge_middleware_wired_before_loop_and_clarification(base_config, atom_home):
    from atom.middleware.clarification import ClarificationMiddleware
    from atom.middleware.loop_detection import LoopDetectionMiddleware
    from atom.middleware.todo_continuation import TodoContinuationMiddleware

    chain = _build_chain(base_config, atom_home)
    types = [type(m).__name__ for m in chain]
    assert "TodoContinuationMiddleware" in types
    nudge_i = next(i for i, m in enumerate(chain) if isinstance(m, TodoContinuationMiddleware))
    loop_i = next(i for i, m in enumerate(chain) if isinstance(m, LoopDetectionMiddleware))
    clar_i = next(i for i, m in enumerate(chain) if isinstance(m, ClarificationMiddleware))
    # Registered EARLIER than loop/clarification -> runs LATER on the reverse after_model unwind,
    # so their jump_to="end" short-circuits before the nudge can fire.
    assert nudge_i < loop_i < clar_i


def test_nudge_middleware_absent_when_disabled(base_config, atom_home):
    from atom.middleware.todo_continuation import TodoContinuationMiddleware

    base_config.todos.continuation_nudge = False
    chain = _build_chain(base_config, atom_home)
    assert not any(isinstance(m, TodoContinuationMiddleware) for m in chain)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_todo_continuation.py -k "wired or absent" -q`
Expected: FAIL — `StopIteration` (no `TodoContinuationMiddleware` in the chain yet).

- [ ] **Step 3: Write minimal implementation**

In `src/atom/agent.py`, inside `_build_middlewares`, add to the local-import block (after `from atom.middleware.thread_data import ThreadDataMiddleware`, ~line 235):

```python
    from atom.middleware.todo_continuation import TodoContinuationMiddleware
```

Then change the chain assembly at ~line 303-312 from:

```python
    chain += [
        TodoListMiddleware(),                            # planning tool — ALWAYS ON
        SubagentMiddleware(runner),                      # delegate_task tool — ALWAYS ON
```

to:

```python
    chain.append(TodoListMiddleware())                   # planning tool — ALWAYS ON
    if cfg.todos.continuation_nudge:                     # nudge the agent to finish incomplete todos
        chain.append(TodoContinuationMiddleware(max_nudges=cfg.todos.max_nudges))
    chain += [
        SubagentMiddleware(runner),                      # delegate_task tool — ALWAYS ON
```

(Leave the rest of the list — `SandboxAuditMiddleware()`, `GuardrailMiddleware(...)`, `ToolErrorHandlingMiddleware()`, `SubagentLimitMiddleware(max_sub)` — unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_todo_continuation.py -q`
Expected: PASS (9 tests). Also run the existing order invariant test to confirm no regression:
Run: `python -m pytest tests/test_agent_smoke.py::test_middleware_order_invariants -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/atom/agent.py tests/test_todo_continuation.py
git commit -m "feat(agent): wire TodoContinuationMiddleware after TodoList (gated on config)"
```

---

### Task 4: End-to-end integration tests

**Files:**
- Test: `tests/test_todo_continuation.py` (append two async integration tests)

**Interfaces:**
- Consumes: `atom.runtime.run_agent`; `tests.conftest.make_prepared`; the wired chain from Task 3.
- Produces: proof that (a) the loop continues then bounds itself when todos stay incomplete, and (b) the per-turn reset clears stale todos across turns on one thread.

**Note on the fake model:** `ScriptedChatModel` clamps to its last response when exhausted (`idx = min(self._i, len-1)`), and `TitleMiddleware` invokes the same scripted model once on the first no-tool-call turn (`title.py:37`). Both tests below are written to be robust to that: the bounded-continuation test asserts on nudge-message count + terminal text (all clamp-tolerant), and the reset test never enters the nudge path.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_todo_continuation.py`:

```python
import pytest

from atom.runtime import run_agent


def _wt_call(content, status, cid):
    # A write_todos tool call setting a single todo to `status`.
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "write_todos",
            "args": {"todos": [{"content": content, "status": status}]},
            "id": cid, "type": "tool_call",
        }],
    )


@pytest.mark.asyncio
async def test_run_continues_then_bounds_when_todos_incomplete(base_config):
    from tests.conftest import make_prepared

    # plan one in_progress todo, then keep trying to stop with a plain answer. The nudge should
    # re-drive the model each stall, then stop after max_nudges (default 2). Clamping makes every
    # post-exhaustion model call return "All done." regardless of index, so this is index-robust.
    prepared = make_prepared([
        _wt_call("build the thing", "in_progress", "wt1"),
        AIMessage(content="All done."),
    ])
    result = await run_agent("do the thing", config=base_config, prepared=prepared)

    # Terminated cleanly (no recursion crash) with the plain answer as the final text.
    assert result.final_text == "All done."
    # Exactly max_nudges continuation nudges were injected -> proves it continued AND was bounded.
    nudges = [
        m for m in result.messages
        if getattr(m, "additional_kwargs", {}).get("lc_source") == _NUDGE_SOURCE
    ]
    assert len(nudges) == base_config.todos.max_nudges == 2
    # The todo is still incomplete (the scripted model never marked it done) -> the run stopped
    # because the nudge budget was exhausted, not because the plan was finished.
    assert any(t["status"] != "completed" for t in result.state.get("todos", []))


@pytest.mark.asyncio
async def test_per_turn_reset_clears_stale_todos_across_turns(base_config):
    from tests.conftest import make_prepared

    # Turn 1: plan + immediately complete one todo, then answer (no nudge — all complete).
    turn1 = make_prepared([
        _wt_call("do it", "completed", "wt1"),
        AIMessage(content="Finished."),
    ])
    r1 = await run_agent("first task", config=base_config, prepared=turn1)
    assert [t["status"] for t in r1.state.get("todos", [])] == ["completed"]

    # Turn 2 on the SAME thread: a plain answer. before_agent must clear turn 1's todos.
    turn2 = make_prepared([AIMessage(content="Hi there.")])
    r2 = await run_agent(
        "unrelated follow-up", config=base_config, prepared=turn2, thread_id=r1.thread_id
    )
    assert r2.final_text == "Hi there."
    assert r2.state.get("todos", []) == []  # stale plan was reset, so no nudge could mis-fire
```

- [ ] **Step 2: Run tests to verify they fail (or drive out issues)**

Run: `python -m pytest tests/test_todo_continuation.py -k "continues_then_bounds or per_turn_reset" -q`
Expected: These exercise already-implemented code, so they may PASS immediately. If either FAILS, treat it as a real defect in Task 2/3 (most likely: `before_agent` not applied to the `todos` channel, or nudge count off) and fix the middleware — do not weaken the assertion.

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest tests/test_todo_continuation.py -q`
Expected: PASS (11 tests).

- [ ] **Step 4: Run the whole project test suite for regressions**

Run: `python -m pytest -q`
Expected: PASS (all prior tests still green; the new middleware is additive and gated).

- [ ] **Step 5: Commit**

```bash
git add tests/test_todo_continuation.py
git commit -m "test(todos): e2e continuation-bound + per-turn reset integration tests"
```

---

## Self-review notes (author)

- **Spec coverage:** per-turn reset → Task 2 `before_agent` + Task 4 reset test; bounded nudge → Task 2 `after_model` + Task 4 continuation test; placement guarantee → Task 3 ordering test; config → Task 1; 10 spec test cases → covered across Tasks 2-4 (fires/no-fire-complete/no-fire-empty/inert-tool-calls/cap/progress-reset/reset/lists-items/ordering/disabled) plus 2 integration tests.
- **Non-goals honored:** no UI surfacing, no `in_progress` prompt edit, no subagent todos, no `todos` reducer.
- **Placeholder scan:** none — every step has concrete code/commands.
- **Type consistency:** `todo_nudge` dict shape `{"count", "completed"}`, `_NUDGE_SOURCE`, and `max_nudges` used identically across Tasks 2-4.
