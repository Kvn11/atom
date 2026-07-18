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
