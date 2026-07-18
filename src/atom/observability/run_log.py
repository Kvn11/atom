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
