import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill_library" / "api-recon-toolkit" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import targets  # noqa: E402

# The ORIGINAL malformed example (missing comma between "request" and "response").
BROKEN = """{
"domain": "my.api.com",
"api": [
    {
    "method": "POST",
    "path": "/some/api/*/?x={}",
    "request": "{'foo':'int' #optional,'bar':'str' #required}"
    "response": "{'zoo':'int'}"
    }
    ]
}"""


def test_normalize_then_load_broken_targets():
    data = json.loads(targets.normalize_jsonish(BROKEN))
    assert data["domain"] == "my.api.com"
    assert len(data["api"]) == 1
    ep = data["api"][0]
    assert ep["method"] == "POST"
    # the pseudo-schema string with in-value '#optional' survives intact
    assert "#optional" in ep["request"]
    assert ep["response"] == "{'zoo':'int'}"


def test_load_targets_accepts_valid_file(tmp_path):
    f = tmp_path / "t.json"
    f.write_text(json.dumps({"domain": "d.com", "api": [
        {"method": "GET", "path": "/a", "request": "{}", "response": "{}"}]}))
    data = targets.load_targets(str(f))
    assert data["domain"] == "d.com"
    assert data["api"][0]["path"] == "/a"


def test_load_targets_recovers_broken_file(tmp_path):
    f = tmp_path / "broken.json"
    f.write_text(BROKEN)
    data = targets.load_targets(str(f))  # must not raise
    assert data["api"][0]["method"] == "POST"


def test_trailing_comma_and_line_comment_tolerated():
    txt = '{\n  "domain": "d",  // hi\n  "api": [\n    {"method":"GET","path":"/x","request":"{}","response":"{}"},\n  ]\n}'
    data = json.loads(targets.normalize_jsonish(txt))
    assert data["api"][0]["path"] == "/x"
