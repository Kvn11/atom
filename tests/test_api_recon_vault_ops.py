import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / "skill_library" / "api-recon-toolkit" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import vault_note  # noqa: E402


def test_put_if_missing_is_noop_on_existing(tmp_path):
    root = str(tmp_path)
    p1, a1 = vault_note.write_note(root, "d.com", "get_root", "endpoint", "ORIGINAL")
    assert a1 == "wrote"
    p2, a2 = vault_note.write_note(root, "d.com", "get_root", "endpoint", "REPLACED", if_missing=True)
    assert a2 == "skipped" and p2.read_text() == "ORIGINAL"     # prior work preserved


def test_put_cli_reports_noop(tmp_path):
    root = str(tmp_path)
    src = tmp_path / "body.md"; src.write_text("BODY", encoding="utf-8")
    def put(*extra):
        return subprocess.run([sys.executable, str(SCRIPTS / "vault_note.py"), "put",
                               "--root", root, "--domain", "d.com", "--slug", "get_root",
                               "--from", str(src), *extra], capture_output=True, text=True)
    assert "OK: wrote" in put().stdout
    assert "NOOP: note exists" in put("--if-missing").stdout


def test_append_creates_then_appends(tmp_path):
    p1 = vault_note.append_section(str(tmp_path), "api.example.com", "get_root", "## Hypotheses\n- H1")
    assert p1 == tmp_path / "api.example.com" / "endpoints" / "get_root.md"
    assert "## Hypotheses" in p1.read_text()
    p2 = vault_note.append_section(str(tmp_path), "api.example.com", "get_root", "## Test log\n- ok")
    body = p2.read_text()
    assert "## Hypotheses" in body and "## Test log" in body      # first section preserved
    assert body.index("## Hypotheses") < body.index("## Test log")


def test_register_blocker_creates_and_dedupes(tmp_path):
    p, _ = vault_note.register_blocker(str(tmp_path), "api.example.com", "no-second-account",
                                       "post_api_v1_users", description="need a 2nd test account")
    assert p == tmp_path / "api.example.com" / "blockers" / "BLK-no-second-account.md"
    body = p.read_text()
    assert "id: BLK-no-second-account" in body and "status: open" in body
    assert "need a 2nd test account" in body
    assert "- [[post_api_v1_users]]" in body
    # a second endpoint hitting the SAME blocker appends; the first is not duplicated
    vault_note.register_blocker(str(tmp_path), "api.example.com", "no-second-account", "get_api_orders")
    body2 = p.read_text()
    assert body2.count("- [[post_api_v1_users]]") == 1
    assert "- [[get_api_orders]]" in body2
    # re-registering the same endpoint is idempotent
    vault_note.register_blocker(str(tmp_path), "api.example.com", "no-second-account", "post_api_v1_users")
    assert p.read_text().count("- [[post_api_v1_users]]") == 1


def test_blocker_status_flip(tmp_path):
    vault_note.register_blocker(str(tmp_path), "d.com", "waf-403", "ep_a", description="WAF blocks probes")
    p, _ = vault_note.register_blocker(str(tmp_path), "d.com", "waf-403", "ep_b", status="removed")
    assert "status: removed" in p.read_text()


def test_concurrent_blocker_registration_no_lost_update(tmp_path):
    # Two processes register the SAME blocker for DIFFERENT endpoints at once; flock must serialize
    # so both affected-endpoint links survive.
    root = str(tmp_path)

    def spawn(endpoint):
        return subprocess.Popen(
            [sys.executable, str(SCRIPTS / "vault_note.py"), "blocker",
             "--root", root, "--domain", "d.com", "--id", "rate-limited", "--endpoint", endpoint])

    a, b = spawn("ep_one"), spawn("ep_two")
    assert a.wait() == 0 and b.wait() == 0
    body = (tmp_path / "d.com" / "blockers" / "BLK-rate-limited.md").read_text()
    assert "- [[ep_one]]" in body and "- [[ep_two]]" in body


def test_blocker_action_created_updated_unchanged(tmp_path):
    root = str(tmp_path)
    _, a1 = vault_note.register_blocker(root, "d.com", "rl", "ep_a", description="rate limited")
    assert a1 == "created"
    _, a2 = vault_note.register_blocker(root, "d.com", "rl", "ep_b")
    assert a2 == "updated"                               # new endpoint linked
    _, a3 = vault_note.register_blocker(root, "d.com", "rl", "ep_a")
    assert a3 == "unchanged"                             # already linked, no status change


def test_blocker_cli_reports_noop(tmp_path):
    root = str(tmp_path)
    def blk(ep):
        return subprocess.run([sys.executable, str(SCRIPTS / "vault_note.py"), "blocker",
                               "--root", root, "--domain", "d.com", "--id", "rl", "--endpoint", ep],
                              capture_output=True, text=True)
    assert "OK: blocker created" in blk("ep_a").stdout
    out = blk("ep_a").stdout
    assert "NOOP:" in out and "already linked" in out


def test_append_kind_recon_targets_recon_md(tmp_path):
    p = vault_note.append_section(str(tmp_path), "d.com", "ignored", "## Recon — 2026-07-21\n- x", kind="recon")
    assert p == tmp_path / "d.com" / "recon.md" and "## Recon" in p.read_text()
