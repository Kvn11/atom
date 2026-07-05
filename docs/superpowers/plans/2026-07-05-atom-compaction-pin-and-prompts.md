# Compaction Instruction-Pin + Production Prompts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Guarantee the user's original instruction survives every compaction verbatim, and raise atom's harness-owned prompts to production grade.

**Architecture:** A new write-once `ThreadState.pinned_instruction` channel is captured from the thread's first `HumanMessage` by a dedicated `InstructionPinMiddleware` (`before_agent`), and re-injected verbatim on every compaction by `PinnedSummarizationMiddleware` (a subclass of LangChain's `SummarizationMiddleware`). The summary prompt and the lead/subagent system prompts are rewritten for clarity and discipline. A `compaction.summary_input_tokens` config controls how much history the summarizer reads.

**Tech Stack:** Python 3.11+, LangChain v1 (`langchain.agents.middleware.SummarizationMiddleware`, `create_agent`, `AgentMiddleware`), Jinja2 (StrictUndefined), pydantic config, pytest (`asyncio_mode = "auto"`).

## Global Constraints

- Run the suite with `.venv/bin/python -m pytest` (NOT `.venv/bin/pytest` — the console script omits repo root from `sys.path` and breaks `from tests.conftest import …`).
- Pin channel name is exactly `pinned_instruction`; it is **write-once** (capture middleware writes only when the channel is empty).
- The injected pin message carries `additional_kwargs={"lc_source": "pinned_instruction"}` and its content is `f"{_PIN_PREFIX}{pinned}"` where `_PIN_PREFIX = "[Standing instruction — the user's original request, preserved verbatim]\n\n"` (em-dash, two trailing newlines).
- `PinnedSummarizationMiddleware` overrides **both** `before_model` and `abefore_model`; the async path is the workflow hot path and must behave identically.
- `build_compaction_middleware(..., trim_tokens=None)`: pass `trim_tokens_to_summarize` to the middleware **only when not None**; unset leaves the library default (4000). `compaction.summary_input_tokens` default is **8000**.
- `summary.md` must keep the literal `{messages}` placeholder (SummarizationMiddleware `.format(messages=…)` contract) and is loaded via `resolve_prompt_ref` (NOT Jinja-rendered).
- Rewritten Jinja prompts (`lead_system.md`, `subagent_general.md`, `subagent_bash.md`) must keep every currently-referenced variable — StrictUndefined raises on any dropped/renamed variable. Do not add new variables.
- Preserve the tool-name tokens the existing lead-prompt test depends on: `read_file`, `bash`, `search_tools` must still appear when those toggles are on.
- `build_compaction_middleware` still returns a `SummarizationMiddleware` (the subclass **is** one) — the existing `isinstance(mw, SummarizationMiddleware)` assertions must keep passing.

---

### Task 1: `pinned_instruction` state channel + `InstructionPinMiddleware`

**Files:**
- Modify: `src/atom/state.py` (add one channel to `ThreadState`)
- Create: `src/atom/middleware/instruction_pin.py`
- Test: `tests/test_instruction_pin.py`

**Interfaces:**
- Consumes: `atom.messages.message_text(message) -> str` (extracts text from str- or list-content messages).
- Produces: `ThreadState["pinned_instruction"]` (a `NotRequired[str]` channel); `InstructionPinMiddleware()` with `before_agent(self, state, runtime) -> dict | None` returning `{"pinned_instruction": <text>}` on first capture, else `None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_instruction_pin.py`:

```python
"""InstructionPinMiddleware captures the thread's first user instruction, once."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from atom.middleware.instruction_pin import InstructionPinMiddleware


def test_captures_first_human_message():
    mw = InstructionPinMiddleware()
    out = mw.before_agent({"messages": [HumanMessage(content="DO THE THING")]}, None)
    assert out == {"pinned_instruction": "DO THE THING"}


def test_captures_text_from_list_content():
    mw = InstructionPinMiddleware()
    content = [{"type": "text", "text": "REAL TASK"}, {"type": "thinking", "thinking": "hmm"}]
    out = mw.before_agent({"messages": [HumanMessage(content=content)]}, None)
    assert out == {"pinned_instruction": "REAL TASK"}


def test_skips_leading_non_human_messages():
    mw = InstructionPinMiddleware()
    msgs = [AIMessage(content="preamble"), HumanMessage(content="THE TASK")]
    out = mw.before_agent({"messages": msgs}, None)
    assert out == {"pinned_instruction": "THE TASK"}


def test_idempotent_when_already_set():
    mw = InstructionPinMiddleware()
    out = mw.before_agent(
        {"messages": [HumanMessage(content="NEW")], "pinned_instruction": "OLD"}, None
    )
    assert out is None


def test_no_human_message_returns_none():
    mw = InstructionPinMiddleware()
    out = mw.before_agent({"messages": [AIMessage(content="hi")]}, None)
    assert out is None


def test_empty_first_human_returns_none():
    mw = InstructionPinMiddleware()
    out = mw.before_agent({"messages": [HumanMessage(content="   ")]}, None)
    assert out is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_instruction_pin.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'atom.middleware.instruction_pin'`.

- [ ] **Step 3: Add the state channel**

In `src/atom/state.py`, add this channel inside `class ThreadState` (e.g. immediately after the `title` channel):

```python
    # The thread's first user instruction, captured once by InstructionPinMiddleware and
    # re-injected verbatim into every compaction so it can never be trimmed or paraphrased
    # away. Write-once; default (LastValue) channel semantics are fine — there is one write.
    pinned_instruction: NotRequired[str]
```

(`NotRequired` is already imported in `state.py`.)

- [ ] **Step 4: Implement the middleware**

Create `src/atom/middleware/instruction_pin.py`:

```python
"""InstructionPinMiddleware — capture the thread's first user instruction, once.

The captured text lands in the ``pinned_instruction`` ThreadState channel (write-once) and is
re-injected verbatim on every compaction by :class:`PinnedSummarizationMiddleware`, so a long
run can never forget what it was asked to do. Runs in the ``before_agent`` group; ordering among
``before_agent`` hooks is immaterial because compaction runs later, in ``before_model``.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from atom.messages import message_text


class InstructionPinMiddleware(AgentMiddleware):
    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        if state.get("pinned_instruction"):        # idempotent: already captured on turn 1
            return None
        for msg in state.get("messages", []):
            if isinstance(msg, HumanMessage):
                text = message_text(msg).strip()
                return {"pinned_instruction": text} if text else None
        return None
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_instruction_pin.py -v`
Expected: PASS (6 passed).

- [ ] **Step 6: Run the full suite to check for regressions**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass (baseline + 6 new).

- [ ] **Step 7: Commit**

```bash
git add src/atom/state.py src/atom/middleware/instruction_pin.py tests/test_instruction_pin.py
git commit -m "feat: pinned_instruction channel + InstructionPinMiddleware capture"
```

---

### Task 2: `PinnedSummarizationMiddleware` re-injection + `trim_tokens`

**Files:**
- Modify: `src/atom/middleware/compaction.py`
- Test: `tests/test_compaction.py` (extend)

**Interfaces:**
- Consumes: `state["pinned_instruction"]` (Task 1); LangChain `SummarizationMiddleware.before_model`/`abefore_model` returning `{"messages": [RemoveMessage(ALL), <summary HumanMessage>, *preserved]}` or `None`.
- Produces: `PinnedSummarizationMiddleware(SummarizationMiddleware)`; `build_compaction_middleware(summarizer_model, *, context_window, ratio=0.5, keep_messages=20, summary_prompt=None, trim_tokens=None)` now returns the subclass.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_compaction.py`:

```python
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from atom.middleware.compaction import PinnedSummarizationMiddleware


def _pinning_mw(summary_text="SUMMARY", keep=2):
    # context_window=2, ratio=0.5 -> trigger ("tokens", 1): any non-empty history summarizes.
    model = ScriptedChatModel(
        responses=[AIMessage(content=summary_text)], profile={"max_input_tokens": 200_000}
    )
    return build_compaction_middleware(
        model, context_window=2, ratio=0.5, keep_messages=keep
    )


def _five_messages(first="ORIGINAL TASK"):
    return [
        HumanMessage(content=first),
        AIMessage(content="a"),
        HumanMessage(content="b"),
        AIMessage(content="c"),
        HumanMessage(content="d"),
    ]


def _pin_msgs(messages):
    return [
        m for m in messages
        if getattr(m, "additional_kwargs", {}).get("lc_source") == "pinned_instruction"
    ]


def test_factory_returns_pinning_subclass():
    mw = _pinning_mw()
    assert isinstance(mw, PinnedSummarizationMiddleware)


def test_pin_injected_verbatim_after_compaction():
    mw = _pinning_mw()
    out = mw.before_model(
        {"messages": _five_messages(), "pinned_instruction": "ORIGINAL TASK"}, None
    )
    result = out["messages"]
    assert isinstance(result[0], RemoveMessage)          # sentinel first
    pins = _pin_msgs(result)
    assert len(pins) == 1
    assert pins[0].content.endswith("ORIGINAL TASK")     # verbatim, with prefix
    assert "Standing instruction" in pins[0].content
    # pin sits BEFORE the library summary message
    pin_i = result.index(pins[0])
    summary_i = next(
        i for i, m in enumerate(result)
        if getattr(m, "additional_kwargs", {}).get("lc_source") == "summarization"
    )
    assert pin_i < summary_i


def test_pin_survives_two_compactions_verbatim():
    pinned = "PIN ME EXACTLY 123"
    mw1 = _pinning_mw(summary_text="SUM1")
    out1 = mw1.before_model(
        {"messages": _five_messages(first=pinned), "pinned_instruction": pinned}, None
    )
    # Apply the RemoveMessage(ALL) sentinel: the surviving list is everything after it.
    kept = [m for m in out1["messages"] if not isinstance(m, RemoveMessage)]
    kept += [AIMessage(content="more"), HumanMessage(content="more2"), AIMessage(content="more3")]
    mw2 = _pinning_mw(summary_text="SUM2")
    out2 = mw2.before_model({"messages": kept, "pinned_instruction": pinned}, None)
    pins = _pin_msgs(out2["messages"])
    assert len(pins) == 1
    assert pins[0].content.endswith(pinned)              # undrifted after 2nd compaction


def test_no_compaction_returns_none_untouched():
    model = ScriptedChatModel(
        responses=[AIMessage(content="S")], profile={"max_input_tokens": 200_000}
    )
    mw = build_compaction_middleware(model, context_window=200_000, ratio=0.5, keep_messages=20)
    out = mw.before_model(
        {"messages": [HumanMessage(content="hi")], "pinned_instruction": "hi"}, None
    )
    assert out is None


def test_empty_pin_no_injection():
    mw = _pinning_mw()
    out = mw.before_model({"messages": _five_messages(), "pinned_instruction": ""}, None)
    assert _pin_msgs(out["messages"]) == []


async def test_async_pin_injection_matches_sync():
    mw = _pinning_mw()
    out = await mw.abefore_model(
        {"messages": _five_messages(first="ASYNC ORIG"), "pinned_instruction": "ASYNC ORIG"}, None
    )
    pins = _pin_msgs(out["messages"])
    assert len(pins) == 1 and pins[0].content.endswith("ASYNC ORIG")


def test_trim_tokens_flows_into_middleware():
    model = ScriptedChatModel(responses=[], profile={"max_input_tokens": 200_000})
    mw = build_compaction_middleware(model, context_window=200_000, trim_tokens=8000)
    assert mw.trim_tokens_to_summarize == 8000


def test_trim_tokens_defaults_to_library_default_when_unset():
    model = ScriptedChatModel(responses=[], profile={"max_input_tokens": 200_000})
    mw = build_compaction_middleware(model, context_window=200_000)
    assert mw.trim_tokens_to_summarize == 4000
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_compaction.py -v`
Expected: FAIL with `ImportError: cannot import name 'PinnedSummarizationMiddleware'`.

- [ ] **Step 3: Implement the subclass and extend the factory**

Replace the body of `src/atom/middleware/compaction.py` with:

```python
"""Compaction driven by the selected model's context window (deviation #5).

We reuse LangChain's built-in ``SummarizationMiddleware`` but compute an *explicit token*
trigger = ``ratio * context_window`` ourselves — where ``context_window`` is resolved
profile-first with a static fallback (:mod:`atom.models.profiles`). This sidesteps the built-in
``("fraction", r)`` path, which reads ``model.profile`` and silently disables itself when the
profile is missing (a real risk for Qwen/DashScope). The built-in already summarizes at a safe
message boundary that never splits a tool-call from its ToolMessage.

``PinnedSummarizationMiddleware`` additionally re-injects the user's original instruction
(captured in the ``pinned_instruction`` state channel) verbatim on every compaction, so it can
never be trimmed or paraphrased away.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, RemoveMessage

_PIN_PREFIX = "[Standing instruction — the user's original request, preserved verbatim]\n\n"


class PinnedSummarizationMiddleware(SummarizationMiddleware):
    """SummarizationMiddleware that re-pins ``state['pinned_instruction']`` on every compaction."""

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._inject_pin(super().before_model(state, runtime), state)

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._inject_pin(await super().abefore_model(state, runtime), state)

    @staticmethod
    def _inject_pin(result: dict[str, Any] | None, state: Any) -> dict[str, Any] | None:
        # None -> no compaction happened; pass through untouched.
        if not result:
            return result
        pinned = (state.get("pinned_instruction") or "").strip()
        if not pinned:
            return result
        msgs = result["messages"]
        # super() returns [RemoveMessage(ALL), <summary HumanMessage>, *preserved]; splice the pin
        # in immediately AFTER the RemoveMessage sentinel and BEFORE the summary.
        insert_at = 1 if (msgs and isinstance(msgs[0], RemoveMessage)) else 0
        pin_msg = HumanMessage(
            content=f"{_PIN_PREFIX}{pinned}",
            additional_kwargs={"lc_source": "pinned_instruction"},
        )
        result["messages"] = [*msgs[:insert_at], pin_msg, *msgs[insert_at:]]
        return result


def build_compaction_middleware(
    summarizer_model: BaseChatModel,
    *,
    context_window: int,
    ratio: float = 0.5,
    keep_messages: int = 20,
    summary_prompt: str | None = None,
    trim_tokens: int | None = None,
) -> SummarizationMiddleware:
    trigger_tokens = max(1, int(ratio * context_window))
    kwargs: dict[str, Any] = {
        "model": summarizer_model,
        "trigger": ("tokens", trigger_tokens),
        "keep": ("messages", keep_messages),
    }
    # A custom, atom-aware summary prompt (preserves mounts/todos/paths). Falls back to the
    # library default when unset. Must contain the ``{messages}`` placeholder.
    if summary_prompt:
        kwargs["summary_prompt"] = summary_prompt
    # How much history the summarizer reads (trim_tokens_to_summarize). Unset -> library default.
    if trim_tokens is not None:
        kwargs["trim_tokens_to_summarize"] = trim_tokens
    return PinnedSummarizationMiddleware(**kwargs)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_compaction.py -v`
Expected: PASS (original 4 + 9 new). The existing `isinstance(mw, SummarizationMiddleware)` checks still hold (subclass).

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/atom/middleware/compaction.py tests/test_compaction.py
git commit -m "feat: PinnedSummarizationMiddleware re-injects instruction verbatim + trim_tokens"
```

---

### Task 3: Config `summary_input_tokens` + wire pin/trim into agent & subagent

**Files:**
- Modify: `src/atom/config/schema.py` (`CompactionConfig`)
- Modify: `config.yaml` (`compaction:` block)
- Modify: `src/atom/agent.py` (`_build_middlewares`: add pin middleware, pass trim, pass trim to runner)
- Modify: `src/atom/subagent.py` (`SubagentRunner`: new field, pin middleware, pass trim)
- Test: `tests/test_compaction.py` (config default) + `tests/test_agent_smoke.py` (wiring)

**Interfaces:**
- Consumes: `InstructionPinMiddleware` (Task 1); `build_compaction_middleware(..., trim_tokens=...)` (Task 2); `cfg.compaction.summary_input_tokens`.
- Produces: `CompactionConfig.summary_input_tokens: int = 8000`; `SubagentRunner.summary_input_tokens: int = 8000`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_compaction.py`:

```python
def test_summary_input_tokens_config_default(base_config):
    assert base_config.compaction.summary_input_tokens == 8000
```

Append to `tests/test_agent_smoke.py`:

```python
def test_instruction_pin_and_trim_are_wired(base_config, atom_home):
    """InstructionPinMiddleware is in the chain and the compaction middleware reads 8000 tokens."""
    from atom.agent import _build_middlewares
    from atom.library import load_library
    from atom.middleware.compaction import PinnedSummarizationMiddleware
    from atom.middleware.instruction_pin import InstructionPinMiddleware
    from atom.sandbox.provider import LocalSandboxProvider

    prepared = make_prepared([])
    profile = base_config.profile("default")
    provider = LocalSandboxProvider()
    library = load_library(str(atom_home))
    chain = _build_middlewares(
        base_config, profile, prepared, provider, str(atom_home), prepared.model, library
    )
    assert any(isinstance(m, InstructionPinMiddleware) for m in chain)
    comp = next(m for m in chain if isinstance(m, PinnedSummarizationMiddleware))
    assert comp.trim_tokens_to_summarize == 8000
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_compaction.py::test_summary_input_tokens_config_default tests/test_agent_smoke.py::test_instruction_pin_and_trim_are_wired -v`
Expected: FAIL — `AttributeError: 'CompactionConfig' object has no attribute 'summary_input_tokens'` and no `InstructionPinMiddleware` in the chain.

- [ ] **Step 3: Add the config field**

In `src/atom/config/schema.py`, extend `CompactionConfig`:

```python
class CompactionConfig(_Base):
    # Fraction of the selected model's context window that triggers summarization (deviation #5).
    ratio: float = 0.5
    keep_messages: int = 20
    # How much conversation history the summarizer reads when building a summary
    # (trim_tokens_to_summarize). Higher = richer summaries at more summarizer cost.
    summary_input_tokens: int = 8000
```

In `config.yaml`, under `compaction:`:

```yaml
compaction:
  ratio: 0.5               # summarize once input reaches 50% of the model's context window
  keep_messages: 20
  summary_input_tokens: 8000   # how much history the summarizer reads (trim_tokens_to_summarize)
```

- [ ] **Step 4: Wire the lead agent**

In `src/atom/agent.py`, `_build_middlewares`:

1. Add the import alongside the other middleware imports:

```python
    from atom.middleware.instruction_pin import InstructionPinMiddleware
```

2. Add `InstructionPinMiddleware()` to the `before_agent` group — insert it right after `UploadsMiddleware(home=home)` in the `chain` list:

```python
        UploadsMiddleware(home=home),                    # register read-only uploads
        InstructionPinMiddleware(),                      # capture first user instruction (pin)
```

3. Pass `trim_tokens` into the compaction builder:

```python
        build_compaction_middleware(                     # 4. 50%-of-window summarization
            summarizer,
            context_window=prepared.context_window,
            ratio=cfg.compaction.ratio,
            keep_messages=cfg.compaction.keep_messages,
            summary_prompt=summary_prompt,
            trim_tokens=cfg.compaction.summary_input_tokens,
        ),
```

4. Pass the trim budget to the subagent runner — in the `SubagentRunner(...)` construction, add:

```python
        summary_input_tokens=cfg.compaction.summary_input_tokens,
```

- [ ] **Step 5: Wire the subagent runner**

In `src/atom/subagent.py`:

1. Add a dataclass field (after `compaction_ratio`):

```python
    compaction_ratio: float = 0.5
    summary_input_tokens: int = 8000
```

2. In `_child_middleware`, import and add the pin middleware, and pass `trim_tokens`:

```python
    def _child_middleware(self) -> list:
        """Minimal resilience so long-running children don't hard-fail on context overflow/loops."""
        from atom.middleware.dangling_tool_call import DanglingToolCallMiddleware
        from atom.middleware.instruction_pin import InstructionPinMiddleware
        from atom.middleware.loop_detection import LoopDetectionMiddleware
        from atom.middleware.tool_error import ToolErrorHandlingMiddleware

        mw: list = [InstructionPinMiddleware(), DanglingToolCallMiddleware()]
        if self.summarizer is not None:
            from atom.middleware.compaction import build_compaction_middleware

            mw.append(
                build_compaction_middleware(
                    self.summarizer,
                    context_window=self.context_window,
                    ratio=self.compaction_ratio,
                    keep_messages=15,
                    trim_tokens=self.summary_input_tokens,
                )
            )
        mw += [ToolErrorHandlingMiddleware(), LoopDetectionMiddleware()]
        return mw
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_compaction.py tests/test_agent_smoke.py -v`
Expected: PASS, including the two new tests.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/atom/config/schema.py config.yaml src/atom/agent.py src/atom/subagent.py tests/test_compaction.py tests/test_agent_smoke.py
git commit -m "feat: wire instruction pin + summary_input_tokens into lead and subagent chains"
```

---

### Task 4: Rewrite `summary.md` (production summary prompt)

**Files:**
- Modify: `src/atom/prompts/summary.md`
- Test: `tests/test_prompts.py` (extend)

**Interfaces:**
- Consumes: nothing new. Loaded via `resolve_prompt_ref("@prompts/summary.md")`; consumed by `SummarizationMiddleware` via `.format(messages=…)`.
- Produces: a summary prompt that keeps `{messages}` and tells the summarizer the instruction is pinned separately.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_prompts.py`:

```python
def test_summary_prompt_keeps_placeholder_and_notes_pin():
    from atom.prompts.render import resolve_prompt_ref

    text = resolve_prompt_ref("@prompts/summary.md")
    assert "{messages}" in text                 # SummarizationMiddleware .format() contract
    assert "pinned" in text.lower()             # tells the summarizer the instruction is pinned
    assert "verbatim" in text.lower()
    assert "## PLAN STATE" in text              # checklist structure present
    assert "## WORKSPACE & FILES" in text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_prompts.py::test_summary_prompt_keeps_placeholder_and_notes_pin -v`
Expected: FAIL (`## PLAN STATE` / pin note absent from the current summary.md).

- [ ] **Step 3: Rewrite the prompt**

Replace the entire contents of `src/atom/prompts/summary.md` with:

```markdown
You are compacting an atom agent's working conversation so it can keep going past its context window. Everything between the <messages> tags below will be REPLACED by the summary you write, so capture exactly what the agent needs to continue — and nothing it can rederive.

The user's original instruction is pinned separately and shown to the agent verbatim on every turn. Do NOT restate or re-summarize it — spend your words on progress since then.

Write a tight, self-contained summary under these headings. Put "None" under any heading with nothing to report.

## PLAN STATE
The current todo list — which items are done, which one is in progress, which remain. If there is no explicit plan, state what has been accomplished and what is left.

## WORKSPACE & FILES
Every virtual path in play — the workspace, uploads, and outputs mounts — and each specific file created, modified, or read, with a few words on what each contains. These are how the agent finds its work; never drop a path.

## DELIVERABLES
Files already presented to the user via present_files, and anything still owed.

## FINDINGS & DECISIONS
Key results, values, and commands that worked, plus choices made and the reason for them, that the agent must not rediscover or reverse. Note rejected approaches and why.

## OPEN QUESTIONS
Anything unresolved or still awaiting a result.

Drop verbose tool output, step-by-step reasoning, and anything already superseded. Be specific and concise.

<messages>
{messages}
</messages>
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_prompts.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/atom/prompts/summary.md tests/test_prompts.py
git commit -m "feat: production summary prompt (checklist form; notes instruction is pinned)"
```

---

### Task 5: Rewrite `lead_system.md` + subagent prompts (production)

**Files:**
- Modify: `src/atom/prompts/lead_system.md`
- Modify: `src/atom/prompts/subagent_general.md`
- Modify: `src/atom/prompts/subagent_bash.md`
- Test: `tests/test_prompts.py` (extend)

**Interfaces:**
- Consumes: existing Jinja variables only (see Global Constraints). `render_lead_system_prompt(...)` and `render_prompt("@prompts/subagent_*.md", ctx)`.
- Produces: same rendered-variable contract, higher-quality content.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_prompts.py`:

```python
def test_lead_prompt_keeps_contract_and_adds_discipline(base_config):
    from atom.agent import render_lead_system_prompt

    prof = base_config.profile("default")
    out = render_lead_system_prompt(
        base_config, prof, "default", {"supports_vision": True},
        frequent_tool_names=["read_file", "bash", "write_todos"],
        has_tool_library=True, has_skill_library=False,
    )
    assert "read_file" in out and "bash" in out          # tool-name contract preserved
    assert "search_tools" in out and "search_skills" not in out
    assert "present_files" in out                         # deliverable discipline
    assert "Plan before you act" in out                   # planning discipline anchor


def test_subagent_prompts_render_and_report_contract():
    from atom.prompts.render import render_prompt

    ctx = {
        "date": "2026-07-05",
        "workspace": "/w",
        "uploads": "/u",
        "outputs": "/o",
        "frequent_tool_names": ["read_file", "write_file"],
    }
    for ref in ("@prompts/subagent_general.md", "@prompts/subagent_bash.md"):
        out = render_prompt(ref, ctx)
        assert "read_file" in out                          # tool list rendered
        assert "self-contained report" in out              # return-value contract
    bash_out = render_prompt("@prompts/subagent_bash.md", ctx)
    assert "bash" in bash_out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_prompts.py -k "lead_prompt_keeps_contract or subagent_prompts_render" -v`
Expected: FAIL (`Plan before you act` / `self-contained report` not in current prompts).

- [ ] **Step 3: Rewrite `lead_system.md`**

Replace the entire contents of `src/atom/prompts/lead_system.md` with:

```markdown
You are {{ agent_name | default("atom") }}, an autonomous agent that completes real tasks by using tools in a live workspace. Today is {{ date }}. Work until the task is genuinely done: plan, act with tools, verify, and hand back a result the user can use.

# Workspace
You operate over a virtual filesystem. Always use these virtual paths:
- `{{ workspace }}` — your working directory for scratch work, code, and intermediate files.
- `{{ uploads }}` — read-only files the user provided.
- `{{ outputs }}` — where finished deliverables belong.
- `{{ skills }}` — reference skill documents.
File tools accept these virtual paths or a path relative to the workspace. Paths outside these mounts are rejected.
{% if frequent_skills %}
# Skills (always available)
{% for s in frequent_skills %}
## {{ s.name }}
{{ s.body }}
{% endfor %}{% endif %}
# How to work
- **Plan before you act.** For anything beyond a single step, call `write_todos` first to lay out a short, concrete plan, then keep it live — mark exactly one item `in_progress`, and flip it to `completed` the moment it's done. Don't batch completions, and don't let the plan drift from what you're actually doing.
- **Do the work with tools, not narration.** You have: {{ frequent_tool_names | join(", ") }}. Reach for a tool instead of describing what you would do.
{% if bash_enabled %}- `bash` runs shell commands in your workspace. Prefer the dedicated file tools (`read_file`, `write_file`, `edit_file`, `ls`, `grep`, `glob`) for file work; use `bash` for running programs, tests, and builds.
{% endif %}- Use `edit_file` for precise in-place edits — its `old_str` must match exactly once unless you pass `replace_all=true`.
- **Verify your work.** After a change, confirm it: read the file back, run the test, or execute the program. Don't claim success you haven't checked.
- **Delegate to stay focused.** Use `delegate_task` to hand a well-scoped subtask (research a directory, run a build, draft a file) to a subagent with a complete, self-contained prompt. Its report is all you get back, so ask for exactly what you need.
- **Surface deliverables.** When you produce something the user should see — a file, a report — save it under `{{ outputs }}` and call `present_files` with the path(s). This is how the result reaches the user; don't skip it.
{% if supports_vision %}- **Look at images** with `view_image` when the task involves a picture, screenshot, or diagram.
{% endif %}{% if has_tool_library or has_skill_library %}
# Discovering more capabilities
Only your most common tools are loaded up front. When a task needs something you don't see:
{% if has_tool_library %}- Call `search_tools("<what you need>")` to find and load a specialized tool from the library.
{% endif %}{% if has_skill_library %}- Call `search_skills("<topic>")` to pull in a step-by-step guide for a specialized workflow.
{% endif %}{% endif %}
# Clarification
If the request is genuinely ambiguous, missing something you cannot discover, or hinges on a decision that is really the user's to make, call `ask_clarification` instead of guessing. It ends your turn; the user's reply resumes the same thread. Don't use it for anything you can reasonably decide or find out yourself.

# Finishing
When the task is complete, write your final answer as a normal message, after your last `write_todos` call rather than in the same turn. Lead with the substance the user asked for — the result itself, not a recap of your steps. Be direct and concrete.
```

- [ ] **Step 4: Rewrite `subagent_general.md`**

Replace the entire contents of `src/atom/prompts/subagent_general.md` with:

```markdown
You are a focused sub-agent handling ONE delegated subtask inside a shared workspace. Today is {{ date }}.

You share the parent agent's workspace:
- `{{ workspace }}` — working directory; files you write here persist for the parent.
- `{{ uploads }}` — read-only inputs.
- `{{ outputs }}` — deliverables.

Do exactly the task you were given with your file tools ({{ frequent_tool_names | join(", ") }}) — nothing more. Don't expand the scope or make decisions that belong to the parent; if the task is underspecified, do the most reasonable thing and state what you assumed.

When you're done, reply with a single, self-contained report: what you found or produced, and the full path of every file you created or changed. That report is your entire return value to the parent — it must stand on its own, because the parent sees nothing else.
```

- [ ] **Step 5: Rewrite `subagent_bash.md`**

Replace the entire contents of `src/atom/prompts/subagent_bash.md` with:

```markdown
You are a focused sub-agent for shell-heavy work inside a shared workspace. Today is {{ date }}.

You have `bash` plus file tools ({{ frequent_tool_names | join(", ") }}) over:
- `{{ workspace }}` — working directory and the cwd for bash.
- `{{ uploads }}` — read-only inputs.
- `{{ outputs }}` — deliverables.

Run the commands the delegated task needs — builds, tests, scripts, inspection. Be deliberate and deterministic: prefer idempotent commands, check the output of each step before the next, and avoid destructive or networked operations unless the task explicitly calls for them.

When you're done, reply with a single self-contained report: the key commands you ran, what they showed, and the full path of every file produced. That report is your entire return value to the parent, so make it complete on its own.
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_prompts.py -v`
Expected: PASS (including the two new tests and the pre-existing `test_default_lead_prompt_renders_and_reflects_toggles`).

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/atom/prompts/lead_system.md src/atom/prompts/subagent_general.md src/atom/prompts/subagent_bash.md tests/test_prompts.py
git commit -m "feat: production lead + subagent system prompts"
```

---

## Notes for the final whole-branch review

- The pin's durability rests on `pinned_instruction` living outside `messages`: `RemoveMessage(REMOVE_ALL_MESSAGES)` only clears the `messages` channel, and the channel is checkpointed, so the pin re-injects identically every cycle. Confirm no code path writes `pinned_instruction` more than once.
- `PinnedSummarizationMiddleware` overrides both sync and async `before_model`; workflows drive `ainvoke`, so `abefore_model` is the real path — verify parity.
- Prompt rewrites must not drop a Jinja variable (StrictUndefined). The render tests cover the default context; spot-check the `frequent_skills`/`bash_enabled=false`/`supports_vision=false` branches render too.
- `user_task.md` is intentionally unchanged (documented passthrough).
```
