import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill_library" / "api-recon-toolkit" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import vault_note  # noqa: E402


def test_slug_matches_endpoint_slug():
    assert vault_note.compute_slug("POST /api/v1/users") == "post_api_v1_users"
    assert vault_note.compute_slug("GET /") == "get_root"


def test_write_endpoint_note_creates_nested_path(tmp_path):
    body = "---\nendpoint: GET /\n---\n# GET /\nhello"
    p = vault_note.write_note(str(tmp_path), "account.vesync.com", "get_root", "endpoint", body)
    assert p == tmp_path / "account.vesync.com" / "endpoints" / "get_root.md"
    assert p.read_text() == body


def test_write_recon_note_goes_to_domain_root(tmp_path):
    p = vault_note.write_note(str(tmp_path), "my.api.com", "ignored", "recon", "# recon")
    assert p == tmp_path / "my.api.com" / "recon.md"
    assert p.read_text() == "# recon"


def test_resolve_root_parses_obsidian_cli_output():
    def fake_runner(cmd):
        # emulate: obsidian vault=X vault info=path -> prints the path
        assert "vault=api-security-assessment" in cmd
        return "/Users/kev/vaults/api-security-assessment\n"
    assert vault_note.resolve_root("api-security-assessment", runner=fake_runner) == \
        "/Users/kev/vaults/api-security-assessment"
