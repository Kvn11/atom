# Self-improving workflow — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A one-click "Improve" button on any finished run that reduces the run into a compact, always-readable run-log, feeds it to a `self-improve` workflow, and produces an improved workflow YAML + a suggestions report as artifacts.

**Architecture:** A new unsandboxed server-side builder (`observability/run_log.py`) turns a run's `run.json` + `chats/` + optional `export.json` into one small JSON (transcript from `chats/`, token/timing/tool-failure metrics from traces). A new API route `POST /api/runs/{id}/self-improve` builds that run-log, stages it (plus the target YAML) as file inputs through the existing `_create_and_enqueue` path, and launches the ordinary `self-improve.yaml` workflow. The UI adds a button that calls the route and navigates to the new run.

**Tech Stack:** Python 3.12, FastAPI, pydantic v2, pytest + httpx `AsyncClient`; React + TypeScript (Vite) for the UI; YAML workflow definitions.

## Global Constraints

- **Run-log must stay readable by the sandboxed agent:** `read_file` refuses > 2,000,000 bytes; keep the run-log small. Individual message `text` bodies are capped at **32,768 characters** with a marker + recorded original length; **no message is ever dropped**.
- **Transcript source is `chats/s<step>__<task>.json`** (atom's clean `{role, text, tool_calls, name}` serialization), never the raw trace message bodies.
- **Trace metrics read only stable top-level fields** (`run_type`, `name`, `error`, `start_time`/`end_time`/`first_token_time`, `prompt_tokens`/`completion_tokens`/`total_tokens`, `extra.metadata`); never parse message content from traces. Missing fields degrade to `None`, never crash.
- **Self-improve workflow name is the constant `SELF_IMPROVE_WORKFLOW = "self-improve"`.** The trigger refuses to self-improve a run of that workflow (recursion guard).
- **Workflow schema is `extra="ignore"`** — the improved YAML must stay within existing fields (`name`, `description`, `inputs[]`, `notes`, `steps[]` → `tasks[]` → `{id?, prompt, model?, thinking?}`); novel keys are silently dropped.
- **Every schema model** lives in `src/atom/workflow/schema.py`; run/store paths in `src/atom/workflow/run_store.py`. Follow existing test style (`tests/conftest.py` fixtures `atom_home`, `base_config`; export tests in `tests/test_export.py`; API tests in `tests/test_workflow_api.py`).
- Run tests with `.venv`: `python -m pytest ...`. Commit after each task.

---

### Task 1: Run-log builder — manifest metrics + `chats/` transcript (no traces)

**Files:**
- Create: `src/atom/observability/run_log.py`
- Test: `tests/test_run_log.py`

**Interfaces:**
- Consumes: `atom.workflow.run_store.RunStore` (`.load(run_id) -> RunManifest`, `.load_chat(run_id, step, task_id) -> list[dict]|None`).
- Produces:
  - `build_run_log(home: str | None, run_id: str) -> dict` — the compact run-log dict (schema below).
  - `run_log_bytes(run_log: dict) -> bytes` — `json.dumps(...).encode("utf-8")`.
  - `MAX_BODY_CHARS: int = 32_768`.
  - Run-log dict schema:
    ```
    {
      "run":   {run_id, workflow, status, created_at, ended_at, duration_s, inputs},
      "steps": [{index, title, status, tasks: [
                  {id, step, model, thinking, status, error, started_at, ended_at, duration_s,
                   tokens: {prompt, completion, total}|null, llm_calls, tool_calls, tool_failures}]}],
      "calls": [],                       # filled by Task 2
      "transcript": [{step, task, role, text, tool_calls, name}],
      "meta": {provider, export_present, export_complete, truncations: [...], notes: [...]}
    }
    ```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_log.py
"""Compact run-log builder: transcript from chats/, metrics from the manifest (+ traces in Task 2)."""
from __future__ import annotations

import json

from atom.observability.run_log import MAX_BODY_CHARS, build_run_log, run_log_bytes
from atom.workflow.run_store import RunManifest, RunStore, StepState, TaskState


def _seed_run(home, run_id="r1"):
    """A 2-step run: step 0 succeeded (poet), step 1 failed (refiner) — like a real halted run."""
    store = RunStore(str(home))
    m = RunManifest(
        run_id=run_id, workflow="parallel-poems", status="halted",
        created_at="2026-07-18T00:00:00", ended_at="2026-07-18T00:01:30",
        inputs={"topic": "rivers"},
        workspace_path=str(store.workspace_dir(run_id)),
        steps=[
            StepState(index=0, title="Draft", status="complete", tasks=[
                TaskState(id="poet_a", thread_id=f"{run_id}:s0:poet_a", model="haiku",
                          status="succeeded", started_at="2026-07-18T00:00:00",
                          ended_at="2026-07-18T00:00:20")]),
            StepState(index=1, title="Refine", status="failed", tasks=[
                TaskState(id="refiner", thread_id=f"{run_id}:s1:refiner", model="haiku",
                          status="failed", error="GraphRecursionError: Recursion limit of 100 reached",
                          started_at="2026-07-18T00:00:20", ended_at="2026-07-18T00:01:30")]),
        ],
    )
    store.create(m)
    store.save_chat(run_id, 0, "poet_a", [
        {"role": "task", "text": "Write a poem about rivers."},
        {"role": "ai", "text": "", "tool_calls": [{"name": "write_file", "args": {"path": "poem_a.md"}}]},
        {"role": "tool", "text": "wrote poem_a.md", "name": "write_file"},
        {"role": "ai", "text": "A river runs..."},
    ])
    store.save_chat(run_id, 1, "refiner", [
        {"role": "task", "text": "Refine every poem."},
        {"role": "ai", "text": "", "tool_calls": [{"name": "read_file", "args": {"path": "poem_a.md"}}]},
    ])
    return store


def test_run_metrics_from_manifest(atom_home):
    _seed_run(atom_home)
    log = build_run_log(str(atom_home), "r1")
    assert log["run"]["workflow"] == "parallel-poems"
    assert log["run"]["status"] == "halted"
    assert log["run"]["duration_s"] == 90.0
    assert log["run"]["inputs"] == {"topic": "rivers"}


def test_task_metrics_and_failure_from_manifest(atom_home):
    _seed_run(atom_home)
    log = build_run_log(str(atom_home), "r1")
    refiner = log["steps"][1]["tasks"][0]
    assert refiner["id"] == "refiner" and refiner["status"] == "failed"
    assert "GraphRecursionError" in refiner["error"]
    assert refiner["duration_s"] == 70.0
    poet = log["steps"][0]["tasks"][0]
    assert poet["status"] == "succeeded" and poet["duration_s"] == 20.0
    assert poet["tokens"] is None            # no traces yet
    assert poet["llm_calls"] == 0 and poet["tool_calls"] == 0


def test_transcript_is_every_chat_message_once(atom_home):
    _seed_run(atom_home)
    log = build_run_log(str(atom_home), "r1")
    tx = log["transcript"]
    # 4 messages for poet_a + 2 for refiner, each exactly once, attributed by step/task.
    assert len(tx) == 6
    assert [e["role"] for e in tx if e["task"] == "poet_a"] == ["task", "ai", "tool", "ai"]
    tool_msg = next(e for e in tx if e["role"] == "tool")
    assert tool_msg["name"] == "write_file" and tool_msg["text"] == "wrote poem_a.md"
    ai_call = next(e for e in tx if e["tool_calls"])
    assert ai_call["tool_calls"][0]["name"] == "write_file"


def test_oversized_body_is_capped_but_message_kept(atom_home):
    store = _seed_run(atom_home)
    huge = "x" * (MAX_BODY_CHARS + 5000)
    store.save_chat("r1", 0, "poet_a", [{"role": "ai", "text": huge}])
    log = build_run_log(str(atom_home), "r1")
    capped = next(e for e in log["transcript"] if e["task"] == "poet_a")
    assert len(capped["text"]) < len(huge)
    assert "truncated" in capped["text"] and str(len(huge)) in capped["text"]
    assert log["meta"]["truncations"] == [
        {"step": 0, "task": "poet_a", "role": "ai", "original_chars": len(huge)}]


def test_meta_flags_no_export(atom_home):
    _seed_run(atom_home)
    log = build_run_log(str(atom_home), "r1")
    assert log["meta"]["export_present"] is False
    assert log["meta"]["export_complete"] is False
    assert log["calls"] == []


def test_run_log_bytes_roundtrips(atom_home):
    _seed_run(atom_home)
    data = run_log_bytes(build_run_log(str(atom_home), "r1"))
    assert isinstance(data, bytes)
    assert json.loads(data)["run"]["run_id"] == "r1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_run_log.py -x -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'atom.observability.run_log'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/atom/observability/run_log.py
"""Reduce a finished run into a compact, always-readable "run-log" for self-improvement analysis.

Unsandboxed (runs in the server process): reads runs/<id>/run.json + chats/ + optional export.json
directly. The transcript comes from chats/ (atom's own deduplicated {role,text,tool_calls,name}
serialization) so no message is ever lost and no fragile provider-message parsing is needed.
Token/timing/tool-failure metrics come from the trace export when present (Task 2), reading only
stable top-level fields.
"""
from __future__ import annotations

import datetime
import json
from typing import Any, Optional

from atom.workflow.run_store import RunManifest, RunStore

MAX_BODY_CHARS = 32_768


def _duration_s(started: Optional[str], ended: Optional[str]) -> Optional[float]:
    if not started or not ended:
        return None
    try:
        a = datetime.datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        b = datetime.datetime.fromisoformat(str(ended).replace("Z", "+00:00"))
        return (b - a).total_seconds()
    except ValueError:
        return None


def _cap_body(text: str) -> tuple[str, Optional[int]]:
    """Trim one oversized body but keep the message; return (text, original_len or None)."""
    if text is None:
        return "", None
    if len(text) <= MAX_BODY_CHARS:
        return text, None
    original = len(text)
    head = text[:MAX_BODY_CHARS]
    return f"{head}\n[truncated {original - MAX_BODY_CHARS} chars — original {original} chars]", original


def _task_row(step_index: int, ts: Any) -> dict:
    return {
        "id": ts.id, "step": step_index, "model": ts.model, "thinking": ts.thinking,
        "status": ts.status, "error": ts.error,
        "started_at": ts.started_at, "ended_at": ts.ended_at,
        "duration_s": _duration_s(ts.started_at, ts.ended_at),
        "tokens": None, "llm_calls": 0, "tool_calls": 0, "tool_failures": 0,
    }


def build_run_log(home: str | None, run_id: str) -> dict:
    store = RunStore(home)
    m: RunManifest = store.load(run_id)

    truncations: list[dict] = []
    transcript: list[dict] = []
    steps: list[dict] = []
    for step in m.steps:
        rows = [_task_row(step.index, t) for t in step.tasks]
        steps.append({"index": step.index, "title": step.title, "status": step.status, "tasks": rows})
        for t in step.tasks:
            chat = store.load_chat(run_id, step.index, t.id) or []
            for msg in chat:
                text, original = _cap_body(msg.get("text", ""))
                if original is not None:
                    truncations.append({"step": step.index, "task": t.id,
                                        "role": msg.get("role"), "original_chars": original})
                transcript.append({
                    "step": step.index, "task": t.id, "role": msg.get("role"),
                    "text": text, "tool_calls": msg.get("tool_calls"), "name": msg.get("name"),
                })

    return {
        "run": {
            "run_id": m.run_id, "workflow": m.workflow, "status": m.status,
            "created_at": m.created_at, "ended_at": m.ended_at,
            "duration_s": _duration_s(m.created_at, m.ended_at), "inputs": m.inputs,
        },
        "steps": steps,
        "calls": [],
        "transcript": transcript,
        "meta": {
            "provider": None, "export_present": False, "export_complete": False,
            "truncations": truncations, "notes": [],
        },
    }


def run_log_bytes(run_log: dict) -> bytes:
    return json.dumps(run_log, ensure_ascii=False).encode("utf-8")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_run_log.py -x -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add src/atom/observability/run_log.py tests/test_run_log.py
git commit -m "feat(run-log): build compact run-log from manifest + chats transcript"
```

---

### Task 2: Run-log builder — trace enrichment (tokens, timings, tool failures)

**Files:**
- Modify: `src/atom/observability/run_log.py`
- Test: `tests/test_run_log.py` (add cases)

**Interfaces:**
- Consumes: `RunStore.export_path(run_id) -> Path` (whole-run `export.json`); the envelope written by `observability/export.build_envelope` — keys `provider`, `complete`, `roots[]`, and each LangSmith root's `extra.metadata` (`step_index`, `task_id`, `is_subagent`), `run_type`, `start_time`/`end_time`/`first_token_time`, `prompt_tokens`/`completion_tokens`/`total_tokens`, `child_runs`, `error`; LangFuse roots' `metadata` + `observations[]` (`type`, `usage`/`usage_details`, `level`, `status_message`, `start_time`/`end_time`).
- Produces: fills `run_log["calls"]`, per-task `tokens`/`llm_calls`/`tool_calls`/`tool_failures`, and `meta.provider`/`export_present`/`export_complete`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_log.py  (append)
import json as _json


def _write_export(store, run_id, provider, roots, complete=True):
    env = {"run_id": run_id, "provider": provider, "complete": complete, "roots": roots}
    store.export_path(run_id).write_text(_json.dumps(env), encoding="utf-8")


def test_langsmith_traces_roll_up_tokens_and_tool_failures(atom_home):
    store = _seed_run(atom_home)
    roots = [
        {  # lead root for step 0 / poet_a
            "extra": {"metadata": {"step_index": 0, "task_id": "poet_a"}},
            "run_type": "chain", "start_time": "2026-07-18T00:00:00", "end_time": "2026-07-18T00:00:20",
            "child_runs": [
                {"run_type": "llm", "name": "haiku", "start_time": "2026-07-18T00:00:01",
                 "end_time": "2026-07-18T00:00:05", "first_token_time": "2026-07-18T00:00:02",
                 "prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500, "error": None},
                {"run_type": "tool", "name": "write_file", "start_time": "2026-07-18T00:00:05",
                 "end_time": "2026-07-18T00:00:06", "error": None},
            ],
        },
        {  # lead root for step 1 / refiner — a failed tool call
            "extra": {"metadata": {"step_index": 1, "task_id": "refiner"}},
            "run_type": "chain", "start_time": "2026-07-18T00:00:20", "end_time": "2026-07-18T00:01:30",
            "child_runs": [
                {"run_type": "llm", "name": "haiku", "start_time": "2026-07-18T00:00:21",
                 "end_time": "2026-07-18T00:00:25", "prompt_tokens": 50000, "completion_tokens": 100,
                 "total_tokens": 50100, "error": None},
                {"run_type": "tool", "name": "read_file", "start_time": "2026-07-18T00:00:25",
                 "end_time": "2026-07-18T00:00:26", "error": "FileNotFoundError: poem_a.md"},
            ],
        },
    ]
    _write_export(store, "r1", "langsmith", roots)
    log = build_run_log(str(atom_home), "r1")

    assert log["meta"]["provider"] == "langsmith"
    assert log["meta"]["export_present"] is True and log["meta"]["export_complete"] is True

    poet = log["steps"][0]["tasks"][0]
    assert poet["tokens"] == {"prompt": 1200, "completion": 300, "total": 1500}
    assert poet["llm_calls"] == 1 and poet["tool_calls"] == 1 and poet["tool_failures"] == 0

    refiner = log["steps"][1]["tasks"][0]
    assert refiner["tokens"]["prompt"] == 50000          # context hotspot lives here
    assert refiner["tool_failures"] == 1

    failed = next(c for c in log["calls"] if c["type"] == "tool" and not c["ok"])
    assert failed["task"] == "refiner" and "FileNotFoundError" in failed["error"]
    llm_call = next(c for c in log["calls"] if c["type"] == "llm" and c["task"] == "poet_a")
    assert llm_call["ttft_s"] == 1.0 and llm_call["duration_s"] == 4.0


def test_subagent_calls_attributed_to_the_task(atom_home):
    store = _seed_run(atom_home)
    roots = [{
        "extra": {"metadata": {"step_index": 0, "task_id": "poet_a"}},
        "run_type": "chain", "child_runs": [
            {"run_type": "chain", "extra": {"metadata": {"is_subagent": True, "agent_role": "general"}},
             "child_runs": [
                 {"run_type": "llm", "name": "haiku", "prompt_tokens": 10, "completion_tokens": 5,
                  "total_tokens": 15, "error": None}]},
        ],
    }]
    _write_export(store, "r1", "langsmith", roots)
    log = build_run_log(str(atom_home), "r1")
    sub = next(c for c in log["calls"] if c["agent"] == "subagent")
    assert sub["task"] == "poet_a" and sub["tokens"]["total"] == 15


def test_incomplete_export_flagged(atom_home):
    store = _seed_run(atom_home)
    _write_export(store, "r1", "langsmith", [], complete=False)
    log = build_run_log(str(atom_home), "r1")
    assert log["meta"]["export_present"] is True and log["meta"]["export_complete"] is False


def test_langfuse_generation_tokens_and_error(atom_home):
    store = _seed_run(atom_home)
    roots = [{
        "metadata": {"step_index": 0, "task_id": "poet_a"},
        "observations": [
            {"type": "GENERATION", "name": "haiku", "start_time": "2026-07-18T00:00:01",
             "end_time": "2026-07-18T00:00:03", "usage_details": {"input": 900, "output": 120, "total": 1020}},
            {"type": "SPAN", "name": "write_file", "level": "ERROR", "status_message": "boom",
             "start_time": "2026-07-18T00:00:03", "end_time": "2026-07-18T00:00:04"},
        ],
    }]
    _write_export(store, "r1", "langfuse", roots)
    log = build_run_log(str(atom_home), "r1")
    poet = log["steps"][0]["tasks"][0]
    assert poet["tokens"] == {"prompt": 900, "completion": 120, "total": 1020}
    assert poet["tool_failures"] == 1
    assert any(c["type"] == "tool" and not c["ok"] and c["error"] == "boom" for c in log["calls"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_run_log.py -x -q -k "traces or subagent or incomplete or langfuse"`
Expected: FAIL (`export_present` is False / `calls` empty — enrichment not implemented)

- [ ] **Step 3: Write minimal implementation**

Add to `src/atom/observability/run_log.py` (helpers above `build_run_log`, and an enrichment pass inside it):

```python
def _ls_tokens(run: dict) -> Optional[dict]:
    p, c, t = run.get("prompt_tokens"), run.get("completion_tokens"), run.get("total_tokens")
    if p is None and c is None and t is None:
        um = (run.get("outputs") or {}).get("usage_metadata") or {}
        p, c, t = um.get("input_tokens"), um.get("output_tokens"), um.get("total_tokens")
    if p is None and c is None and t is None:
        return None
    p, c = p or 0, c or 0
    return {"prompt": p, "completion": c, "total": t if t is not None else p + c}


def _lf_tokens(usage: dict) -> Optional[dict]:
    if not usage:
        return None
    p = usage.get("input") or usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    c = usage.get("output") or usage.get("completion_tokens") or usage.get("output_tokens") or 0
    t = usage.get("total") or usage.get("total_tokens")
    if not p and not c and not t:
        return None
    return {"prompt": p, "completion": c, "total": t if t is not None else p + c}


def _blank_acc() -> dict:
    return {"prompt": 0, "completion": 0, "total": 0, "llm_calls": 0, "tool_calls": 0,
            "tool_failures": 0, "seen_tokens": False}


def _walk_langsmith(run: dict, step, task, agent, calls: list, acc: dict) -> None:
    rt = run.get("run_type")
    dur = _duration_s(run.get("start_time"), run.get("end_time"))
    if rt == "llm":
        toks = _ls_tokens(run)
        calls.append({"step": step, "task": task, "agent": agent, "type": "llm",
                      "name": run.get("name"), "duration_s": dur,
                      "ttft_s": _duration_s(run.get("start_time"), run.get("first_token_time")),
                      "tokens": toks, "ok": run.get("error") is None, "error": run.get("error")})
        acc["llm_calls"] += 1
        if toks:
            acc["seen_tokens"] = True
            acc["prompt"] += toks["prompt"]; acc["completion"] += toks["completion"]; acc["total"] += toks["total"]
    elif rt == "tool":
        err = run.get("error")
        calls.append({"step": step, "task": task, "agent": agent, "type": "tool",
                      "name": run.get("name"), "duration_s": dur, "ttft_s": None,
                      "tokens": None, "ok": err is None, "error": err})
        acc["tool_calls"] += 1
        if err:
            acc["tool_failures"] += 1
    for child in run.get("child_runs") or []:
        cmeta = (child.get("extra") or {}).get("metadata") or {}
        cagent = "subagent" if cmeta.get("is_subagent") else agent
        _walk_langsmith(child, step, task, cagent, calls, acc)


def _walk_langfuse(trace: dict, calls: list, accs: dict) -> None:
    meta = trace.get("metadata") or {}
    step, task = meta.get("step_index"), meta.get("task_id")
    agent = "subagent" if meta.get("is_subagent") else "lead"
    acc = accs.setdefault((step, task), _blank_acc())
    for ob in trace.get("observations") or []:
        dur = _duration_s(ob.get("start_time"), ob.get("end_time"))
        if ob.get("type") == "GENERATION":
            toks = _lf_tokens(ob.get("usage_details") or ob.get("usage") or {})
            calls.append({"step": step, "task": task, "agent": agent, "type": "llm",
                          "name": ob.get("name"), "duration_s": dur,
                          "ttft_s": _duration_s(ob.get("start_time"), ob.get("completion_start_time")),
                          "tokens": toks, "ok": ob.get("level") != "ERROR",
                          "error": ob.get("status_message")})
            acc["llm_calls"] += 1
            if toks:
                acc["seen_tokens"] = True
                acc["prompt"] += toks["prompt"]; acc["completion"] += toks["completion"]; acc["total"] += toks["total"]
        else:
            is_err = ob.get("level") == "ERROR"
            calls.append({"step": step, "task": task, "agent": agent, "type": "tool",
                          "name": ob.get("name"), "duration_s": dur, "ttft_s": None,
                          "tokens": None, "ok": not is_err, "error": ob.get("status_message")})
            acc["tool_calls"] += 1
            if is_err:
                acc["tool_failures"] += 1


def _enrich(run_log: dict, store: RunStore, run_id: str) -> None:
    path = store.export_path(run_id)
    if not path.is_file():
        return
    try:
        env = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        run_log["meta"]["notes"].append("export.json present but unreadable")
        return
    provider = env.get("provider")
    run_log["meta"]["provider"] = provider
    run_log["meta"]["export_present"] = True
    run_log["meta"]["export_complete"] = bool(env.get("complete"))
    calls: list = []
    accs: dict = {}
    for root in env.get("roots") or []:
        if provider == "langfuse":
            _walk_langfuse(root, calls, accs)
        else:
            meta = (root.get("extra") or {}).get("metadata") or {}
            step, task = meta.get("step_index"), meta.get("task_id")
            agent = "subagent" if meta.get("is_subagent") else "lead"
            acc = accs.setdefault((step, task), _blank_acc())
            _walk_langsmith(root, step, task, agent, calls, acc)
    run_log["calls"] = calls
    for step in run_log["steps"]:
        for row in step["tasks"]:
            acc = accs.get((step["index"], row["id"]))
            if not acc:
                continue
            row["llm_calls"] = acc["llm_calls"]; row["tool_calls"] = acc["tool_calls"]
            row["tool_failures"] = acc["tool_failures"]
            if acc["seen_tokens"]:
                row["tokens"] = {"prompt": acc["prompt"], "completion": acc["completion"], "total": acc["total"]}
```

Then call `_enrich(...)` at the end of `build_run_log`, before the `return`. Restructure `build_run_log` so it builds the dict into a variable `log`, then:

```python
    log = { ... the dict currently returned ... }
    _enrich(log, store, run_id)
    return log
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_run_log.py -q`
Expected: PASS (all cases, including Task 1's)

- [ ] **Step 5: Commit**

```bash
git add src/atom/observability/run_log.py tests/test_run_log.py
git commit -m "feat(run-log): enrich with token/timing/tool-failure metrics from traces"
```

---

### Task 3: Trigger endpoint `POST /api/runs/{run_id}/self-improve`

**Files:**
- Modify: `src/atom/api/app.py`
- Test: `tests/test_self_improve_api.py`

**Interfaces:**
- Consumes: `build_run_log`, `run_log_bytes` (Task 1/2); `_create_and_enqueue(wf, inputs, files)` (existing, `app.py:103`, `files={name: (filename, bytes)}`); `load_workflow`, `workflows_dir` (`workflow/schema.py`); `store.load(run_id)`; the export dispatch already in `export_traces`.
- Produces: route returning `{"run_id": <new>, "status": "queued"}` (202); module constant `SELF_IMPROVE_WORKFLOW = "self-improve"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_self_improve_api.py
"""POST /api/runs/{id}/self-improve: validate, build run-log, stage inputs, enqueue a new run."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage

from atom.api.app import create_app
from atom.workflow.engine import WorkflowEngine
from atom.workflow.run_store import RunManifest, RunStore, StepState, TaskState
from tests.conftest import make_prepared


@asynccontextmanager
async def _client(app):
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


def _provider(td, sd, wf):
    """Scripted so the queue worker can drain the enqueued self-improve run offline."""
    return make_prepared([AIMessage(content="done")])


def _engine(base_config):
    return WorkflowEngine(base_config, prepared_provider=_provider)


def _install_self_improve(home):
    d = home / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / "self-improve.yaml").write_text(
        "name: self-improve\n"
        "inputs:\n"
        "  - name: run_log\n    type: file\n    required: true\n"
        "  - name: target_workflow\n    type: file\n    required: true\n"
        "  - name: workflow_name\n    required: true\n"
        "  - name: source_run_id\n    required: true\n"
        "  - name: run_status\n    required: false\n"
        "steps:\n  - title: Analyze\n    tasks:\n      - id: a\n        prompt: \"read {{ run_log }}\"\n"
    )


def _install_target(home, name="parallel-poems"):
    d = home / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.yaml").write_text(
        f"name: {name}\nsteps:\n  - title: S\n    tasks:\n      - id: t1\n        prompt: hi\n")


def _seed_terminal_run(home, run_id="r1", workflow="parallel-poems", status="halted"):
    store = RunStore(str(home))
    m = RunManifest(
        run_id=run_id, workflow=workflow, status=status, created_at="2026-07-18T00:00:00",
        ended_at="2026-07-18T00:01:00", workspace_path=str(store.workspace_dir(run_id)),
        steps=[StepState(index=0, title="S", status="failed", tasks=[
            TaskState(id="t1", thread_id=f"{run_id}:s0:t1", status="failed", error="boom",
                      started_at="2026-07-18T00:00:00", ended_at="2026-07-18T00:01:00")])],
    )
    store.create(m)
    store.save_chat(run_id, 0, "t1", [{"role": "task", "text": "do it"}, {"role": "ai", "text": "boom"}])
    return store


@pytest.mark.asyncio
async def test_self_improve_happy_path(base_config, atom_home):
    _install_self_improve(atom_home)
    _install_target(atom_home)
    _seed_terminal_run(atom_home)
    app = create_app(base_config, engine=_engine(base_config))
    async with _client(app) as client:
        r = await client.post("/api/runs/r1/self-improve")
        assert r.status_code == 202, r.text
        new_id = r.json()["run_id"]
        assert new_id and new_id != "r1"
        # the new run is a self-improve run whose inputs point back at the source
        m = (await client.get(f"/api/runs/{new_id}")).json()
        assert m["workflow"] == "self-improve"
        assert m["inputs"]["source_run_id"] == "r1"
        assert m["inputs"]["workflow_name"] == "parallel-poems"
        # both file inputs were staged into the new run's uploads dir
        store = RunStore(str(atom_home))
        up = store.uploads_dir(new_id)
        assert (up / "run_log.json").exists()
        assert (up / "target_workflow.yaml").exists()
        run_log = json.loads((up / "run_log.json").read_text())
        assert run_log["run"]["run_id"] == "r1" and run_log["run"]["status"] == "halted"


@pytest.mark.asyncio
async def test_self_improve_requires_terminal_run(base_config, atom_home):
    _install_self_improve(atom_home)
    _install_target(atom_home)
    _seed_terminal_run(atom_home, status="running")
    app = create_app(base_config, engine=_engine(base_config))
    async with _client(app) as client:
        r = await client.post("/api/runs/r1/self-improve")
        assert r.status_code == 409


@pytest.mark.asyncio
async def test_self_improve_recursion_guard(base_config, atom_home):
    _install_self_improve(atom_home)
    _seed_terminal_run(atom_home, workflow="self-improve")
    app = create_app(base_config, engine=_engine(base_config))
    async with _client(app) as client:
        r = await client.post("/api/runs/r1/self-improve")
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_self_improve_run_not_found(base_config, atom_home):
    _install_self_improve(atom_home)
    app = create_app(base_config, engine=_engine(base_config))
    async with _client(app) as client:
        r = await client.post("/api/runs/ghost/self-improve")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_self_improve_missing_target_yaml(base_config, atom_home):
    _install_self_improve(atom_home)
    _seed_terminal_run(atom_home, workflow="deleted-wf")   # no deleted-wf.yaml on disk
    app = create_app(base_config, engine=_engine(base_config))
    async with _client(app) as client:
        r = await client.post("/api/runs/r1/self-improve")
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_self_improve_missing_workflow_definition_503(base_config, atom_home):
    _install_target(atom_home)                              # target exists, self-improve.yaml does NOT
    _seed_terminal_run(atom_home)
    app = create_app(base_config, engine=_engine(base_config))
    async with _client(app) as client:
        r = await client.post("/api/runs/r1/self-improve")
        assert r.status_code == 503
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_self_improve_api.py -x -q`
Expected: FAIL with 404/405 (route not defined)

- [ ] **Step 3: Write minimal implementation**

In `src/atom/api/app.py`, add the import and constant near the top (after the existing imports):

```python
from atom.observability.run_log import build_run_log, run_log_bytes
from atom.workflow.schema import MissingInputError, list_workflows, load_workflow, workflows_dir

SELF_IMPROVE_WORKFLOW = "self-improve"
```

(Extend the existing `from atom.workflow.schema import ...` line to include `workflows_dir` rather than adding a duplicate import.)

Add this route inside `create_app`, after `export_traces` / `download_export` (it closes over `_create_and_enqueue`, `store`, `cfg`, `engine`):

```python
    @app.post("/api/runs/{run_id}/self-improve", status_code=202)
    def self_improve(run_id: str) -> dict:
        """Analyze a finished run and launch the self-improve workflow on it.

        Reduces the run to a compact run-log, stages it + the target workflow's YAML as file
        inputs, and enqueues a new `self-improve` run through the ordinary submission path.
        """
        try:
            manifest = store.load(run_id)
        except FileNotFoundError:
            raise HTTPException(404, "run not found")
        if manifest.status not in ("complete", "halted"):
            raise HTTPException(409, "run is not finished yet")
        if manifest.workflow == SELF_IMPROVE_WORKFLOW:
            raise HTTPException(400, "cannot self-improve the self-improvement workflow")

        target_path = workflows_dir(cfg.home) / f"{manifest.workflow}.yaml"
        if not target_path.is_file():
            raise HTTPException(404, f"workflow '{manifest.workflow}' no longer exists on disk")
        target_yaml = target_path.read_bytes()

        try:
            wf = load_workflow(SELF_IMPROVE_WORKFLOW, cfg.home)
        except FileNotFoundError:
            raise HTTPException(503, f"'{SELF_IMPROVE_WORKFLOW}.yaml' is not installed in "
                                     f"{workflows_dir(cfg.home)} — install it to enable self-improvement")

        _ensure_export(run_id)                      # best-effort; never blocks
        run_log = build_run_log(cfg.home, run_id)

        inputs = {
            "workflow_name": manifest.workflow,
            "source_run_id": run_id,
            "run_status": manifest.status,
        }
        files = {
            "run_log": ("run_log.json", run_log_bytes(run_log)),
            "target_workflow": (f"{manifest.workflow}.yaml", target_yaml),
        }
        return _create_and_enqueue(wf, inputs, files)
```

Add the best-effort export helper inside `create_app` (above the route), reusing the provider dispatch already proven in `export_traces`:

```python
    def _ensure_export(run_id: str) -> None:
        """Generate the run's trace export if it isn't on disk. Best-effort: any failure
        (no traces, missing keys, backend error) is swallowed — the run-log degrades gracefully."""
        if store.export_path(run_id).is_file():
            return
        provider = cfg.observability.provider
        if provider is None:
            provider = "langsmith" if cfg.observability.enabled else "none"
        try:
            if provider == "langfuse":
                from atom.observability import langfuse_export as export_mod
                from atom.observability.provider import resolve_langfuse_keys
                public, secret, _ = resolve_langfuse_keys(cfg.observability)
                if not (public and secret):
                    return
                export_mod.export_run(cfg.home, run_id, cfg=cfg)
            elif provider != "none":
                from atom.observability import export as export_mod
                if not cfg.observability.project:
                    return
                export_mod.export_run(cfg.home, run_id, project=cfg.observability.project, cfg=cfg)
        except Exception:  # noqa: BLE001 — export is optional enrichment; never block the trigger
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_self_improve_api.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Run the full suite to check nothing regressed**

Run: `python -m pytest tests/test_workflow_api.py tests/test_run_log.py tests/test_self_improve_api.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/atom/api/app.py tests/test_self_improve_api.py
git commit -m "feat(api): add POST /runs/{id}/self-improve trigger endpoint"
```

---

### Task 4: The `self-improve.yaml` workflow definition

**Files:**
- Create: `workflows/self-improve.yaml`
- Test: `tests/test_self_improve_workflow.py`

**Interfaces:**
- Consumes: `WorkflowDef.model_validate` / `load_workflow` (`workflow/schema.py`); the run-log input schema (Task 1/2) and `{{ run_log }}`, `{{ target_workflow }}`, `{{ workflow_name }}`, `{{ source_run_id }}`, `{{ run_status }}`, `{{ workspace }}`, `{{ outputs }}` template vars.
- Produces: a valid `WorkflowDef` named `self-improve` whose two steps emit `improved-<name>.yaml` + `suggestions.md` artifacts.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_self_improve_workflow.py
"""The shipped self-improve.yaml is a valid WorkflowDef with the inputs the trigger stages."""
from __future__ import annotations

from pathlib import Path

import yaml

from atom.workflow.schema import WorkflowDef

_YAML = Path(__file__).resolve().parents[1] / "workflows" / "self-improve.yaml"


def test_self_improve_yaml_is_valid_workflowdef():
    wf = WorkflowDef.model_validate(yaml.safe_load(_YAML.read_text()))
    assert wf.name == "self-improve"
    names = {i.name: i for i in wf.inputs}
    assert names["run_log"].type == "file" and names["run_log"].required
    assert names["target_workflow"].type == "file" and names["target_workflow"].required
    assert {"workflow_name", "source_run_id", "run_status"} <= set(names)


def test_self_improve_has_analyze_then_improve_steps():
    wf = WorkflowDef.model_validate(yaml.safe_load(_YAML.read_text()))
    assert len(wf.steps) == 2
    assert len(wf.steps[0].tasks) >= 2            # parallel analysis tasks
    assert len(wf.steps[1].tasks) == 1            # single synthesis task
    # the synthesis prompt names both deliverables
    synth = wf.steps[1].tasks[0].prompt
    assert "improved-" in synth and "suggestions.md" in synth
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_self_improve_workflow.py -x -q`
Expected: FAIL with `FileNotFoundError` (the YAML doesn't exist)

- [ ] **Step 3: Write the workflow file**

```yaml
# workflows/self-improve.yaml — copy to $ATOM_HOME/workflows/ to enable the Improve button.
name: self-improve
description: >
  Analyze a finished workflow run and produce an improved workflow file plus a
  suggestions report. Triggered by the "Improve" button on a finished run.
inputs:
  - name: run_log
    type: file
    required: true
    description: Compact run-log JSON for the source run (metrics + transcript).
  - name: target_workflow
    type: file
    required: true
    description: The current YAML of the workflow that produced the source run.
  - name: workflow_name
    required: true
    description: Name of the workflow being improved.
  - name: source_run_id
    required: true
    description: The run_id that was analyzed.
  - name: run_status
    required: false
    description: Final status of the source run (complete or halted).
steps:
  - title: Analyze
    description: Three analysts read the run-log + target workflow and write findings to the workspace.
    tasks:
      - id: failures_and_tools
        prompt: |
          You are reviewing run {{ source_run_id }} of the "{{ workflow_name }}" workflow
          (final status: {{ run_status }}).

          Read the compact run-log at {{ run_log }} and the target workflow YAML at
          {{ target_workflow }}. The run-log has: run/step/task status + errors + durations,
          a `calls` list (per LLM/tool call: tokens, duration, and tool errors), and a
          `transcript` (every message once, with tool calls and tool results).

          Write {{ workspace }}/analysis/failures.md covering:
          - Which tasks failed and the precise reason (quote the manifest `error` and any tool
            error text from `calls` / `tool`-role transcript messages).
          - Every failed tool call: the tool name, the error, and the LIKELY cause.
          - For each issue, tag it [workflow] (fixable by editing the YAML: prompt, step/task
            layout, model, thinking) or [harness] (a bug, unclear tool description, or verbose
            tool output that a YAML edit cannot fix).
          Then call present_files on {{ workspace }}/analysis/failures.md.
        thinking: medium
      - id: bottlenecks_and_context
        prompt: |
          Read the run-log at {{ run_log }} and the target workflow at {{ target_workflow }}.

          Write {{ workspace }}/analysis/performance.md covering:
          - Bottlenecks: rank steps/tasks by duration (`duration_s`) and by token use
            (`tokens.total` and `tokens.prompt`). Call out the slowest and most expensive.
          - Context hotspots: which steps/tasks/messages were overly context-consuming
            (large `tokens.prompt`, or transcript entries in meta.truncations). For each, state
            the root cause and whether it is [workflow]-fixable (e.g. re-reading a huge file every
            turn, un-summarized fan-in, a task doing too much) or [harness].
          - If the run-log meta says export_present is false, note that token data was unavailable
            and reason from durations + transcript only.
          Then call present_files on {{ workspace }}/analysis/performance.md.
        thinking: medium
      - id: structure_and_prompts
        prompt: |
          Read the run-log at {{ run_log }} and the target workflow at {{ target_workflow }}.

          Write {{ workspace }}/analysis/structure.md covering:
          - What went WELL (keep these — do not regress them).
          - Workflow design: step/task decomposition, missed parallelization (tasks that could
            run concurrently in one step), model and thinking choices per task, and prompt
            clarity/ambiguity. Tag each observation [workflow] or [harness].
          Then call present_files on {{ workspace }}/analysis/structure.md.
        thinking: medium
  - title: Improve
    description: Synthesize the findings into an improved workflow file and a suggestions report.
    tasks:
      - id: synthesize
        prompt: |
          Read every file in {{ workspace }}/analysis/, the target workflow at
          {{ target_workflow }}, and the run-log at {{ run_log }}.

          Produce TWO deliverables and present both with present_files.

          1. {{ workspace }}/improved-{{ workflow_name }}.yaml — an improved version of the
             target workflow that applies the [workflow]-tagged fixes (better step/task
             decomposition, parallelization, model/thinking choices, clearer prompts, and fixes
             for context hotspots). Constraints:
             - It MUST be a valid atom workflow: top-level `name`, optional `description`,
               `inputs`, `steps`; each step has a `title` and a non-empty `tasks` list; each task
               has a `prompt` and optional `id`, `model`, `thinking`. Keep the same `inputs` as
               the target unless a change is clearly justified.
             - Only use these fields — any other key is silently ignored by the loader, so do not
               invent new ones.
             - Preserve what went well. Every change must trace to a finding.

          2. {{ workspace }}/suggestions.md — for the [harness]-tagged issues that a YAML edit
             cannot fix (unclear tool descriptions, harness bugs, verbose tool output, config or
             observability gaps). Include:
             - A short summary: what went well, what went wrong, top bottlenecks, top context hotspots.
             - A changelog: every change you made to the workflow YAML and the finding that drove it.

          Base everything on run {{ source_run_id }} (status {{ run_status }}).
        thinking: high
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_self_improve_workflow.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Install into $ATOM_HOME and commit**

```bash
cp workflows/self-improve.yaml "${ATOM_HOME:-$HOME/.atom}/workflows/self-improve.yaml"
git add workflows/self-improve.yaml tests/test_self_improve_workflow.py
git commit -m "feat(workflows): add self-improve workflow definition"
```

---

### Task 5: UI — Improve button, API client, navigation

**Files:**
- Modify: `atom-ui/src/api.ts`
- Modify: `atom-ui/src/RunView.tsx`
- Modify: `atom-ui/src/App.tsx`

**Interfaces:**
- Consumes: `POST /api/runs/{id}/self-improve` (Task 3) returning `{run_id, status}`; existing `setView({name:"run", runId})` navigation in `App.tsx`.
- Produces: `api.selfImprove(id)`; an "Improve" button + result banner in `RunView`; an `onOpenRun` prop threaded from `App.tsx`.

- [ ] **Step 1: Add the API client method**

In `atom-ui/src/api.ts`, inside the `api` object (after `exportRun`):

```typescript
  selfImprove: (id: string): Promise<{ run_id: string; status: string }> =>
    fetch(`/api/runs/${id}/self-improve`, { method: "POST" }).then(async (r) => {
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || `self-improve failed (${r.status})`);
      return data as { run_id: string; status: string };
    }),
```

- [ ] **Step 2: Add the button + banner + prop in RunView**

In `atom-ui/src/RunView.tsx`:

Change the component signature (line 101) to accept `onOpenRun`:

```typescript
export function RunView({ runId, onBack, onOpenRun }:
  { runId: string; onBack: () => void; onOpenRun?: (id: string) => void }) {
```

Add state next to the export state (after line 108):

```typescript
  const [improving, setImproving] = useState(false);
  const [improveMsg, setImproveMsg] = useState<{ text: string; kind: "ok" | "err"; runId?: string } | null>(null);
```

Add the handler next to `runExport` (after line 166):

```typescript
  const runSelfImprove = async () => {
    setImproving(true);
    setImproveMsg(null);
    try {
      const res = await api.selfImprove(runId);
      setImproveMsg({ text: "Self-improvement run started", kind: "ok", runId: res.run_id });
    } catch (e) {
      setImproveMsg({ text: e instanceof Error ? e.message : String(e), kind: "err" });
    } finally {
      setImproving(false);
    }
  };
```

Add the button in the `.run-status` block, right after the "Export run" button (after line 187), hidden on self-improve runs:

```typescript
            {manifest.workflow !== "self-improve" && (
              <button className="btn-sm"
                disabled={!(manifest.status === "complete" || manifest.status === "halted") || improving}
                onClick={() => runSelfImprove()}
                title={(manifest.status === "complete" || manifest.status === "halted")
                  ? "Analyze this run and draft an improved workflow"
                  : "Available once the run finishes"}>
                {improving ? "Improving…" : "Improve"}
              </button>
            )}
```

Add the banner right after the existing `exportMsg` banner block (after line 200):

```typescript
      {improveMsg && (
        <div className={`export-banner ${improveMsg.kind}`}>
          <span className="export-text">{improveMsg.text}</span>
          {improveMsg.runId && onOpenRun && (
            <button className="export-dl" onClick={() => onOpenRun(improveMsg.runId!)}>
              View self-improvement run →
            </button>
          )}
          <button className="export-x" onClick={() => setImproveMsg(null)} title="Dismiss">✕</button>
        </div>
      )}
```

- [ ] **Step 3: Thread the prop from App.tsx**

In `atom-ui/src/App.tsx`, update the RunView render (line 50):

```typescript
        {view.name === "run" && (
          <RunView runId={view.runId} onBack={() => setView({ name: "runs" })}
            onOpenRun={(id) => setView({ name: "run", runId: id })} />
        )}
```

- [ ] **Step 4: Typecheck / build the UI**

Run: `cd atom-ui && npm run build`
Expected: build succeeds with no TypeScript errors (a `dist/` is produced).

- [ ] **Step 5: Commit**

```bash
git add atom-ui/src/api.ts atom-ui/src/RunView.tsx atom-ui/src/App.tsx
git commit -m "feat(ui): add Improve button that launches self-improvement and opens the new run"
```

---

### Task 6: End-to-end smoke + docs

**Files:**
- Modify: `README.md` (document the feature)
- Test: manual E2E described below

- [ ] **Step 1: Full backend suite**

Run: `python -m pytest -q`
Expected: PASS (no regressions across the whole suite).

- [ ] **Step 2: Manual end-to-end (with the app running)**

1. Start the API + UI (however the project normally runs — e.g. `uvicorn atom.api.app:create_app --factory` + `atom-ui` dev server, or the built SPA served by the API).
2. Run any workflow (e.g. `parallel-poems`) to completion or failure.
3. On its run view, confirm the **Improve** button is enabled once the run is terminal.
4. Click it → a banner shows "Self-improvement run started → View it"; click through.
5. Watch the `self-improve` run stream Step 1 (3 analysts) then Step 2 (synthesis).
6. Open its Deliverables: confirm `improved-<name>.yaml` and `suggestions.md` are present.
7. Confirm the emitted YAML re-validates: `python -c "import yaml,sys; from atom.workflow.schema import WorkflowDef; WorkflowDef.model_validate(yaml.safe_load(open(sys.argv[1]).read()))" <path-to-improved.yaml>` exits 0.
8. Confirm the Improve button is **absent** on the `self-improve` run's own view.

- [ ] **Step 3: Document in README**

Add a short "Self-improving workflows" subsection under the workflows docs: what the Improve button does, that it emits an improved YAML + suggestions as artifacts to review and copy in by hand, that `self-improve.yaml` must be installed in `$ATOM_HOME/workflows/`, and that token/context-consumption detail requires observability to have been enabled for the source run.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: document the self-improving workflow feature"
```

---

## Self-Review

**1. Spec coverage:**
- Improve button on terminal runs (complete|halted), hidden on self-improve runs → Task 5. ✓
- Compact run-log, transcript from `chats/`, no message loss, 32KB body cap → Task 1. ✓
- Token/timing/tool-failure metrics from traces, both provider shapes, graceful degrade → Task 2. ✓
- Trigger endpoint: terminal gate (409), recursion guard (400), missing target YAML (404), missing self-improve.yaml (503), stage both file inputs via `_create_and_enqueue`, best-effort export → Task 3. ✓
- Analysis dimensions (failures + tool causes, bottlenecks + context hotspots, structure/prompts + what-went-well) and both deliverables (improved YAML + suggestions) with [workflow]/[harness] tagging → Task 4. ✓
- Navigation to the new run → Task 5. ✓
- Docs + E2E → Task 6. ✓

**2. Placeholder scan:** No "TBD"/"handle edge cases"/"similar to". Every code step shows complete code; the one prose step (README) is documentation, not code. ✓

**3. Type consistency:** `build_run_log(home, run_id)` and `run_log_bytes(run_log)` used identically in Task 1/2 (definition) and Task 3 (consumption). `_create_and_enqueue(wf, inputs, files)` with `files={name: (filename, bytes)}` matches `app.py:103`. Run-log dict keys (`run`, `steps[].tasks[].tokens/llm_calls/...`, `calls`, `transcript`, `meta.export_present`) are consistent across builder tests and the endpoint test. `SELF_IMPROVE_WORKFLOW = "self-improve"` used in the endpoint and the recursion guard; the UI guards on the literal `"self-improve"`. ✓

**Notes for the implementer:**
- Task 2 restructures `build_run_log` to assign the dict to `log`, call `_enrich(log, store, run_id)`, then `return log`. Don't leave the original `return {...}` in place.
- The trace field shapes in Task 2 are modeled on the documented LangSmith `Run`/LangFuse `ObservationsView` dumps (no real export exists on disk to fixture against); the parser defaults every missing field to `None` and never raises, so a shape surprise degrades to "no token data" rather than a 500.
