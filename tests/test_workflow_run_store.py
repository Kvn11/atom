"""Run manifest persistence (atomic) and chat snapshots."""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from atom.workflow.run_store import (
    ArtifactRef, RunManifest, RunStore, StepState, TaskState, serialize_messages,
)


def _manifest(run_id, ws):
    return RunManifest(
        run_id=run_id, workflow="demo", inputs={"topic": "x"},
        created_at="2026-07-03T00:00:00", workspace_path=str(ws),
        steps=[StepState(index=0, title="Draft",
                         tasks=[TaskState(id="t1", thread_id=f"{run_id}:s0:t1")])],
    )


def test_create_and_load_roundtrip(atom_home):
    store = RunStore(str(atom_home))
    m = store.create(_manifest("r1", store.workspace_dir("r1")))
    assert store.workspace_dir("r1").is_dir()
    loaded = store.load("r1")
    assert loaded.run_id == "r1"
    assert loaded.steps[0].tasks[0].thread_id == "r1:s0:t1"


def test_save_is_atomic_no_tmp_left(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("r2", store.workspace_dir("r2")))
    m = store.load("r2")
    m.status = "running"
    store.save(m)
    assert store.load("r2").status == "running"
    leftovers = list(store.run_dir("r2").glob("*.tmp"))
    assert leftovers == []


def test_list_sorted_desc(atom_home):
    store = RunStore(str(atom_home))
    a = _manifest("ra", store.workspace_dir("ra")); a.created_at = "2026-07-01T00:00:00"
    b = _manifest("rb", store.workspace_dir("rb")); b.created_at = "2026-07-02T00:00:00"
    store.create(a); store.create(b)
    assert [m.run_id for m in store.list()] == ["rb", "ra"]


def test_chat_snapshot_roundtrip(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("r3", store.workspace_dir("r3")))
    assert store.load_chat("r3", 0, "t1") is None
    store.save_chat("r3", 0, "t1", [{"role": "ai", "text": "hi"}])
    assert store.load_chat("r3", 0, "t1") == [{"role": "ai", "text": "hi"}]


def test_serialize_messages_shape():
    msgs = [
        HumanMessage(content="do it"),
        AIMessage(content="", tool_calls=[{"name": "write_file", "args": {"path": "p"}, "id": "c1", "type": "tool_call"}]),
        ToolMessage(content="ok", tool_call_id="c1", name="write_file"),
        AIMessage(content="done"),
    ]
    out = serialize_messages(msgs)
    # The opening prompt of a workflow task is authored by the automated workflow, not a human.
    assert out[0] == {"role": "task", "text": "do it"}
    assert out[1]["tool_calls"] == [{"name": "write_file", "args": {"path": "p"}}]
    assert out[2]["role"] == "tool" and out[2]["name"] == "write_file"
    assert out[3]["text"] == "done"


def test_serialize_messages_relabels_only_first_human():
    # Only the opening prompt becomes "task"; injected mid-turn human notes (skill activations,
    # view-image blocks) keep the "human" role.
    msgs = [
        HumanMessage(content="the task prompt"),
        AIMessage(content="thinking"),
        HumanMessage(content="[Activated skill 'foo']"),
        AIMessage(content="done"),
    ]
    out = serialize_messages(msgs)
    assert out[0]["role"] == "task"
    assert out[2]["role"] == "human"


def test_capture_artifacts_copies_and_snapshots(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("rc", store.workspace_dir("rc")))
    src = store.workspace_dir("rc") / "poem.md"
    src.write_text("draft\n")
    refs = store.capture_artifacts(
        "rc", 0, "poet_a",
        [{"path": "/mnt/user-data/outputs/poem.md", "physical": str(src)}],
    )
    assert len(refs) == 1
    assert refs[0].name == "poem.md"
    assert refs[0].rel == "s0__poet_a/poem.md"
    assert refs[0].size == len("draft\n")
    dest = store.artifacts_dir("rc") / "s0__poet_a" / "poem.md"
    assert dest.read_text() == "draft\n"
    src.write_text("CHANGED\n")               # snapshot immutability
    assert dest.read_text() == "draft\n"


def test_capture_artifacts_skips_missing_source(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("rm", store.workspace_dir("rm")))
    refs = store.capture_artifacts(
        "rm", 0, "t1",
        [{"path": "/mnt/x/gone.md", "physical": str(store.workspace_dir("rm") / "nope.md")}],
    )
    assert refs == []


def test_capture_artifacts_disambiguates_collision(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("rd", store.workspace_dir("rd")))
    a = store.workspace_dir("rd") / "a.md"; a.write_text("A\n")
    sub = store.workspace_dir("rd") / "sub"; sub.mkdir()
    b = sub / "a.md"; b.write_text("B\n")
    refs = store.capture_artifacts("rd", 0, "t1", [
        {"path": "/mnt/x/a.md", "physical": str(a)},
        {"path": "/mnt/y/a.md", "physical": str(b)},
    ])
    assert sorted(r.name for r in refs) == ["a-1.md", "a.md"]


def test_artifact_path_confined(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("rp", store.workspace_dir("rp")))
    ok = store.artifact_path("rp", "s0__t1/f.md")
    assert ok is not None and str(ok).endswith("/artifacts/s0__t1/f.md")
    assert store.artifact_path("rp", "../../run.json") is None


import json as _json


def _save_with_status(store, run_id, status, created_at,
                      step_status="complete", task_status="succeeded"):
    m = _manifest(run_id, store.workspace_dir(run_id))
    m.created_at = created_at
    m.status = status
    m.steps[0].status = step_status
    m.steps[0].tasks[0].status = task_status
    store.create(m)
    return m


def test_summary_json_written_on_save(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("sm1", store.workspace_dir("sm1")))
    sp = store.run_dir("sm1") / "summary.json"
    assert sp.exists()
    data = _json.loads(sp.read_text())
    assert data["run_id"] == "sm1" and data["tasks_total"] == 1


def test_list_summaries_counts_filter_pagination(atom_home):
    store = RunStore(str(atom_home))
    _save_with_status(store, "r_run", "running", "2026-07-01T00:00:00",
                      step_status="running", task_status="running")
    _save_with_status(store, "r_done", "complete", "2026-07-02T00:00:00")
    _save_with_status(store, "r_halt", "halted", "2026-07-03T00:00:00",
                      step_status="failed", task_status="failed")

    page = store.list_summaries()
    assert page["counts"] == {"active": 1, "complete": 1, "halted": 1, "cancelled": 0}
    assert page["total"] == 3
    assert [i["run_id"] for i in page["items"]] == ["r_halt", "r_done", "r_run"]

    active = store.list_summaries(status="active")
    assert [i["run_id"] for i in active["items"]] == ["r_run"] and active["total"] == 1

    pg2 = store.list_summaries(limit=1, offset=1)
    assert [i["run_id"] for i in pg2["items"]] == ["r_done"] and pg2["total"] == 3


def test_list_summaries_fallback_when_summary_missing(atom_home):
    store = RunStore(str(atom_home))
    _save_with_status(store, "r_x", "complete", "2026-07-01T00:00:00")
    (store.run_dir("r_x") / "summary.json").unlink()
    page = store.list_summaries()
    assert [i["run_id"] for i in page["items"]] == ["r_x"]


def test_list_summaries_clamps_negative_offset(atom_home):
    store = RunStore(str(atom_home))
    _save_with_status(store, "r1", "complete", "2026-07-01T00:00:00")  # oldest
    _save_with_status(store, "r2", "complete", "2026-07-02T00:00:00")  # newest
    # offset=-1 must NOT wrap-around-slice (which would return just the oldest run);
    # it clamps to 0 and returns both, newest first.
    page = store.list_summaries(offset=-1)
    assert [i["run_id"] for i in page["items"]] == ["r2", "r1"]
    assert page["total"] == 2


def _mk(run_id, status, enqueued_at=None, created_at="2026-07-12T00:00:00"):
    from atom.workflow.run_store import RunManifest
    return RunManifest(
        run_id=run_id, workflow="wf", status=status,
        created_at=created_at, enqueued_at=enqueued_at, workspace_path="/x", steps=[],
    )


def test_queue_dir_path(atom_home):
    from atom.workflow.run_store import RunStore
    store = RunStore(str(atom_home))
    assert store.queue_dir == atom_home / "workflows" / "queue"


def test_queued_run_ids_fifo_and_interrupted(atom_home):
    from atom.workflow.run_store import RunStore
    store = RunStore(str(atom_home))
    store.create(_mk("b", "queued", enqueued_at="2026-07-12T00:00:02.000000"))
    store.create(_mk("a", "queued", enqueued_at="2026-07-12T00:00:01.000000"))
    store.create(_mk("done", "complete"))
    store.create(_mk("mid", "running"))
    store.create(_mk("new", "pending"))

    assert store.queued_run_ids() == ["a", "b"]           # FIFO by enqueued_at
    assert set(store.interrupted_run_ids()) == {"mid", "new"}


def test_uploads_dir_created_by_create(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("ru", store.workspace_dir("ru")))
    assert store.uploads_dir("ru") == store.run_dir("ru") / "uploads"
    assert store.uploads_dir("ru").is_dir()


def test_save_upload_deterministic_name_and_virtual_path(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("rup", store.workspace_dir("rup")))
    vpath = store.save_upload("rup", "document", "q3-results.pdf", b"PDFDATA")
    assert vpath == "/mnt/user-data/uploads/document.pdf"
    assert (store.uploads_dir("rup") / "document.pdf").read_bytes() == b"PDFDATA"


def test_manifest_uploads_path_roundtrips(atom_home):
    store = RunStore(str(atom_home))
    m = _manifest("rpp", store.workspace_dir("rpp"))
    m.uploads_path = str(store.uploads_dir("rpp"))
    store.create(m)
    assert store.load("rpp").uploads_path == str(store.uploads_dir("rpp"))


# --- run_id path-traversal confinement (defense-in-depth at the path-construction chokepoint) ---

def test_run_dir_rejects_unsafe_run_id(atom_home):
    store = RunStore(str(atom_home))
    for bad in ("../evil", "a/b", "..", ".", "x\\y", "n\x00ull", ""):
        with pytest.raises(ValueError):
            store.run_dir(bad)
    assert store.run_dir("abc123def456") == store.runs_dir / "abc123def456"   # legit id unaffected


def test_load_blocks_run_id_traversal(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("victim", store.workspace_dir("victim")))
    assert store.load("victim").run_id == "victim"                            # legit load works
    # runs_dir / "../runs/victim" collapses back onto the victim run — must NOT resolve to it
    with pytest.raises(FileNotFoundError):
        store.load("../runs/victim")


def test_load_chat_blocks_run_id_traversal(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("victim", store.workspace_dir("victim")))
    store.save_chat("victim", 0, "t1", [{"role": "ai", "text": "secret"}])
    assert store.load_chat("victim", 0, "t1") == [{"role": "ai", "text": "secret"}]
    assert store.load_chat("../runs/victim", 0, "t1") is None                 # traversal blocked


def test_artifact_path_blocks_run_id_traversal(atom_home):
    store = RunStore(str(atom_home))
    store.create(_manifest("victim", store.workspace_dir("victim")))
    art = store.artifacts_dir("victim") / "s0__t1"
    art.mkdir(parents=True, exist_ok=True)
    (art / "secret.txt").write_text("x")
    assert store.artifact_path("victim", "s0__t1/secret.txt") is not None
    assert store.artifact_path("../runs/victim", "s0__t1/secret.txt") is None  # traversal blocked


def test_cancel_marker_roundtrip(atom_home):
    store = RunStore(str(atom_home))
    store.create(RunManifest(
        run_id="r1", workflow="wf", created_at="2026-07-18T00:00:00",
        workspace_path=str(store.workspace_dir("r1")), steps=[]))
    assert store.cancel_requested("r1") is False
    store.write_cancel_marker("r1", "2026-07-18T00:00:00.000000")
    assert store.cancel_requested("r1") is True
    store.clear_cancel_marker("r1")
    assert store.cancel_requested("r1") is False


def test_cancel_requested_false_for_unsafe_id(atom_home):
    store = RunStore(str(atom_home))
    assert store.cancel_requested("../evil") is False
    store.clear_cancel_marker("../evil")   # no-op, must not raise


def test_list_summaries_counts_cancelled(atom_home):
    store = RunStore(str(atom_home))
    store.create(RunManifest(
        run_id="c1", workflow="wf", status="cancelled", created_at="2026-07-18T00:00:00",
        workspace_path=str(store.workspace_dir("c1")), steps=[]))
    page = store.list_summaries(status="all")
    assert page["counts"]["cancelled"] == 1
    assert page["counts"]["active"] == 0
