"""End-to-end: an uploaded file is shared across a run and readable from the {{ uploads }} mount."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from langchain_core.messages import AIMessage

import atom.workflow.engine as engine_mod
from atom.workflow.engine import WorkflowEngine
from atom.workflow.schema import WorkflowDef
from tests.conftest import make_prepared

UP = "/mnt/user-data/uploads"


def test_example_summarize_doc_workflow_valid():
    data = yaml.safe_load(Path("workflows/summarize-doc.yaml").read_text())
    wf = WorkflowDef.model_validate(data)
    assert wf.name == "summarize-doc"
    doc = next(i for i in wf.inputs if i.name == "document")
    assert doc.type == "file" and doc.required is True


def _file_wf() -> WorkflowDef:
    return WorkflowDef.model_validate({
        "name": "docwf",
        "inputs": [{"name": "document", "type": "file", "required": True}],
        "steps": [{"title": "Read", "tasks": [{"id": "t1", "prompt": "read {{ document }}"}]}],
    })


@pytest.mark.asyncio
async def test_uploaded_file_readable_from_mount_and_path_resolved(base_config, atom_home, monkeypatch):
    captured = {}
    real = engine_mod.run_agent

    async def spy(prompt, **kwargs):
        captured["prompt"] = prompt
        return await real(prompt, **kwargs)

    monkeypatch.setattr(engine_mod, "run_agent", spy)

    def provider(td, sd, wf):
        return make_prepared([
            AIMessage(content="", tool_calls=[{
                "name": "read_file",
                "args": {"description": "r", "path": f"{UP}/document.txt"},
                "id": "c1", "type": "tool_call"}]),
            AIMessage(content="done"),
        ])

    engine = WorkflowEngine(base_config, prepared_provider=provider)
    run_id = "run_e2e"
    engine.create_run(_file_wf(), {"document": f"{UP}/document.txt"}, run_id, "2026-07-15T00:00:00")
    engine.store.save_upload(run_id, "document", "myreport.txt", b"the tide returns\n")

    manifest = await engine.execute(run_id)

    assert manifest.status == "complete"
    assert f"{UP}/document.txt" in captured["prompt"]          # {{ document }} resolved to the mount path
    chat = engine.store.load_chat(run_id, 0, "t1")
    tool_texts = "\n".join(m["text"] for m in chat if m["role"] == "tool")
    assert "the tide returns" in tool_texts                    # agent read the file from the shared mount
