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
    """Roll `export.json` into `run_log` in place; any read/shape surprise degrades, never raises.

    A missing export leaves Task 1's shape untouched (`calls == []`, `export_present=False`).
    Once the export is confirmed present+parsed, each root in `roots` is walked independently:
    a downstream shape surprise on one root (e.g. it isn't a dict, or a child field isn't the
    expected shape) must not turn into a 500 for the caller, and must not discard metrics already
    collected from other roots that walked cleanly — it degrades to "skip that root" with a note,
    per the "never crash on trace data" rule.
    """
    path = store.export_path(run_id)
    if not path.is_file():
        return
    try:
        env = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(env, dict):
            raise ValueError("export.json root is not an object")
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
        if not isinstance(root, dict):
            run_log["meta"]["notes"].append("export.json root had an unexpected shape; skipped")
            continue
        try:
            if provider == "langfuse":
                _walk_langfuse(root, calls, accs)
            else:
                meta = (root.get("extra") or {}).get("metadata") or {}
                step, task = meta.get("step_index"), meta.get("task_id")
                agent = "subagent" if meta.get("is_subagent") else "lead"
                acc = accs.setdefault((step, task), _blank_acc())
                _walk_langsmith(root, step, task, agent, calls, acc)
        except (AttributeError, TypeError):
            run_log["meta"]["notes"].append("export.json root had an unexpected shape; skipped")
            continue
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

    log = {
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
    _enrich(log, store, run_id)
    return log


def run_log_bytes(run_log: dict) -> bytes:
    return json.dumps(run_log, ensure_ascii=False).encode("utf-8")
