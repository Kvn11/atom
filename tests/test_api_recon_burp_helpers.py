import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skill_library" / "api-recon-toolkit" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _burp  # noqa: E402

EXAMPLE_XML = str(Path(__file__).resolve().parents[1] / "examples" / "account.vesync.com.xml")

# The real authorizeCode JWT from the example capture (alg=HS256, aud=22134806).
VESYNC_JWT = (
    "eyJhbGciOiJIUzI1NiJ9."
    "eyJpc3MiOiJ2ZXN5bmMuY29tIiwiYXVkIjoiMjIxMzQ4MDYiLCJ0ZXJtaW5hbElkIjoiMjI0OTQ4"
    "NzVkM2Q3NTNjMTZhZjliZTg0MDgzNjhlZDM1IiwiZXhwIjoxNzc5Njc2MTE3MjQzLCJpYXQiOjE3"
    "Nzk2NzUyMTcyNDMsImp0aSI6ImRiYTUxYTVlOGE2YzQ0NmRhMDFmZjVkY2QyMzU4OWViIn0."
    "K8_5EbzSIglbdhrL2t1X8Tm5EX9idTZa-pet8e9-uPg"
)


def test_endpoint_slug_basic():
    assert _burp.endpoint_slug("POST", "/api/v1/users") == "post_api_v1_users"
    assert _burp.endpoint_slug("GET", "/") == "get_root"
    # query string is dropped
    assert _burp.endpoint_slug("GET", "/?action=delAccount&x=1") == "get_root"
    # wildcards / punctuation collapse to single underscores
    assert _burp.endpoint_slug("POST", "/some/api/*/?x={}") == "post_some_api"


def test_decode_jwt_claims_no_raw_token():
    d = _burp.decode_jwt(VESYNC_JWT)
    assert d is not None
    assert d["alg"] == "HS256"
    assert d["payload"]["aud"] == "22134806"
    assert d["payload"]["iss"] == "vesync.com"
    assert d["sig_bytes"] == 32  # HS256 signature is 32 bytes
    # the raw token must never be echoed back inside the decoded structure
    assert VESYNC_JWT not in repr(d)


def test_decode_jwt_rejects_garbage():
    assert _burp.decode_jwt("not.a.jwt") is None
    assert _burp.decode_jwt("") is None


def test_find_jwts_extracts_from_noisy_text():
    # the capture prefixes the JWT with digits ("30410011eyJ...") — still found
    found = _burp.find_jwts("authorizeCode=30410011" + VESYNC_JWT + "&lang=en")
    assert VESYNC_JWT in found


def test_is_asset_filters_static_but_keeps_api_doc():
    items = list(_burp.iter_items(EXAMPLE_XML))
    assert len(items) == 23
    assets = [it for it in items if _burp.is_asset(it)]
    apis = [it for it in items if not _burp.is_asset(it)]
    # 22 static assets (js/css/svg/config.js), 1 kept (the GET / delAccount HTML doc)
    assert len(apis) == 1
    assert apis[0].index == 22
    assert len(assets) == 22
