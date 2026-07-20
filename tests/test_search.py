"""search_skills is discovery-only; load_skill is the sole loader."""
from __future__ import annotations

from types import SimpleNamespace

from atom.library import load_library, register_index
from atom.tools.search import load_skill, search_skills
from tests.conftest import seed_library


def _runtime(home, state=None):
    return SimpleNamespace(context={"home": str(home)}, state=state or {}, tool_call_id="tc1")


def test_search_skills_lists_and_does_not_promote(atom_home):
    seed_library(atom_home)  # adds skill_library/pdf-extract
    register_index(str(atom_home), load_library(str(atom_home)))
    cmd = search_skills.func(_runtime(atom_home), query="extract text from a pdf")
    assert "promoted_skills" not in cmd.update              # discovery only
    msg = cmd.update["messages"][0].content
    assert "pdf-extract" in msg and "load_skill" in msg


def test_load_skill_promotes_known_skill(atom_home):
    d = atom_home / "skills" / "demo-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: demo-skill\ndescription: A demo skill\n---\nBODY")
    cmd = load_skill.func(_runtime(atom_home), name="demo-skill")
    assert cmd.update["promoted_skills"] == ["demo-skill"]
    assert "Loaded skill 'demo-skill'" in cmd.update["messages"][0].content


def test_load_skill_rejects_unknown_and_traversal(atom_home):
    assert "promoted_skills" not in load_skill.func(_runtime(atom_home), name="nope").update
    assert "promoted_skills" not in load_skill.func(_runtime(atom_home), name="../etc/passwd").update


def test_load_skill_message_names_skill_library_mount(atom_home):
    seed_library(atom_home)  # adds skill_library/pdf-extract
    cmd = load_skill.func(_runtime(atom_home), name="pdf-extract")
    msg = str(cmd.update["messages"][0].content)
    assert "/mnt/skill_library/pdf-extract/" in msg


def test_load_skill_message_names_skills_mount_for_always_on(atom_home):
    d = atom_home / "skills" / "demo-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: demo-skill\ndescription: x\n---\nBODY")
    cmd = load_skill.func(_runtime(atom_home), name="demo-skill")
    msg = str(cmd.update["messages"][0].content)
    assert "/mnt/skills/demo-skill/" in msg
    assert "/mnt/skill_library/" not in msg
