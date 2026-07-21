# API Security & Privacy Assessment Workflow — Implementation Plan (Step 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Step 1 ("Setup") of a production atom workflow that runs an authorized API security assessment on a weak model (gemini-2.5-pro): a shipped `api-recon-toolkit` skill of LLM-friendly slicing CLIs + a two-parallel-task workflow YAML.

**Architecture:** Port the proven stdlib Burp parser from the `analyze-burp-requests` skill into `skill_library/api-recon-toolkit/scripts/_burp.py`, add a few helpers, and wrap it in three small argparse CLIs (`burp.py`, `targets.py`, `vault_note.py`) that truncate/slice by default so a weak model never loads whole files. A `workflows/api-security-assessment.yaml` runs two parallel Gemini tasks (capture-recon + SDK build) whose prompts drive those CLIs explicitly. Everything is Python-3 stdlib-only and unit-tested with pytest (no live model in CI).

**Tech Stack:** Python 3.12 stdlib (argparse/json/base64/gzip/zlib/xml.etree/urllib), pytest, atom workflow YAML, the device `obsidian` CLI (only invoked at runtime, mocked in tests).

## Global Constraints

- **Model:** every task uses `model: gemini-pro` (→ gemini-2.5-pro). **Never** Gemini 3. Set `model`+`thinking` per-task; do not require a `--profile` or `config.yaml` edit.
- **Stdlib only** in all shipped scripts — no third-party imports (target device may lack them). `brotli` is used only inside a `try/except` that already exists in the ported parser.
- **Slice, never dump:** every CLI truncates output by default and exposes `--limit`; bodies shown as key-trees (`--keys`) unless a body slice is explicitly requested.
- **No secrets in notes/output:** decode JWTs to claims + signature byte-length; **never** print or store a raw token/cookie value beyond a short truncated sample.
- **Weak-model ergonomics:** scripts invoked by absolute `/mnt/skill_library/api-recon-toolkit/scripts/<x>.py` path; guidance lives in the task prompts, not only in `SKILL.md`.
- **Shared vs isolated dirs:** reusable artifacts (SDK, harvested values) → `{{ workspace }}`; final deliverables → `{{ outputs }}` + `present_files`.
- **Vault:** one Obsidian vault `api-security-assessment`, split by domain at root: `<domain>/recon.md` and `<domain>/endpoints/<slug>.md`. Notes written with `write_file` then filed by `vault_note.py` (never large inline `content=`).
- **Branch:** all work on `feat/api-security-assessment-workflow` (already checked out). Commit after each task.
- **Test layout:** tests in `tests/`, importing scripts by adding the scripts dir to `sys.path`. Run with `python -m pytest`.

---

### Task 1: Shared parser `_burp.py` (port + recon helpers)

**Files:**
- Create: `skill_library/api-recon-toolkit/scripts/_burp.py`
- Test: `tests/test_api_recon_burp_helpers.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces (imported by Tasks 2 & 4):
  - Ported verbatim from `burp_parse.py`: `iter_items(xml_path) -> Iterator[BurpItem]`; `BurpItem` (fields `index,source,time,url,host,port,protocol,method,path,extension,status,responselength,mimetype,comment`, props `.request -> HttpMessage`, `.response -> HttpMessage|None`, `.url_path()`, `.query_params()`); `HttpMessage` (`.start_line,.headers,.body`, `.header(n)`, `.cookies()`, `.content_type()`, `.is_json()`, `.is_text()`, `.body_text()`, `.body_json()`); `parse_http_message(raw)`, `truncate_text(text,limit)`, `json_keys_summary(value)`, `safe_path_component(s)`.
  - New helpers: `is_asset(item: BurpItem) -> bool`; `find_jwts(text: str) -> list[str]`; `decode_jwt(token: str) -> dict | None` (returns `{"alg","header","payload","sig_bytes"}`); `endpoint_slug(method: str, path: str) -> str`.

- [ ] **Step 1: Scaffold the skill dir and port the parser base**

```bash
mkdir -p skill_library/api-recon-toolkit/scripts
cp ~/.claude/skills/analyze-burp-requests/scripts/burp_parse.py skill_library/api-recon-toolkit/scripts/_burp.py
# sanity: the port imports and exposes iter_items
python3 -c "import sys; sys.path.insert(0,'skill_library/api-recon-toolkit/scripts'); import _burp; print(_burp.iter_items, _burp.HttpMessage, _burp.json_keys_summary)"
```
Expected: three object reprs print, no ImportError. (If the source path is absent, the file content is the module reproduced in `analyze-burp-requests/scripts/burp_parse.py`; recreate it verbatim.)

- [ ] **Step 2: Write the failing test for the new helpers**

Create `tests/test_api_recon_burp_helpers.py`:
```python
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
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/test_api_recon_burp_helpers.py -q`
Expected: FAIL — `AttributeError: module '_burp' has no attribute 'endpoint_slug'`.

- [ ] **Step 4: Append the new helpers to `_burp.py`**

Append to `skill_library/api-recon-toolkit/scripts/_burp.py`:
```python

# --- recon helpers (added for api-recon-toolkit) ----------------------------

import base64 as _base64
from urllib.parse import urlsplit as _urlsplit

# Static-asset detection: assets are GET responses with an asset extension or mimetype.
_ASSET_EXTS = {
    "js", "mjs", "css", "map", "svg", "png", "jpg", "jpeg", "gif", "webp",
    "ico", "woff", "woff2", "ttf", "otf", "eot",
}
_ASSET_MIMES = {"script", "css", "image", "font"}


def is_asset(item: "BurpItem") -> bool:
    """True when the item is a static asset (noise), not an API/XHR/document request."""
    if (item.method or "").upper() != "GET":
        return False
    ext = (item.extension or "").lower().lstrip(".")
    if ext in _ASSET_EXTS:
        return True
    return (item.mimetype or "").strip().lower() in _ASSET_MIMES


_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


def find_jwts(text: str) -> list[str]:
    """Return every JWT-looking substring (header.payload.signature), de-duplicated in order."""
    if not text:
        return []
    seen: list[str] = []
    for m in _JWT_RE.findall(text):
        if m not in seen:
            seen.append(m)
    return seen


def _b64url(seg: str) -> bytes:
    return _base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def decode_jwt(token: str) -> dict | None:
    """Decode a JWT into {alg, header, payload, sig_bytes}. Never returns the raw token.

    Returns None if the token is not a well-formed three-segment JWT.
    """
    if not token or token.count(".") != 2:
        return None
    h, p, s = token.split(".")
    try:
        header = json.loads(_b64url(h))
        payload = json.loads(_b64url(p))
        sig_bytes = len(_b64url(s))
    except Exception:
        return None
    return {"alg": header.get("alg"), "header": header, "payload": payload, "sig_bytes": sig_bytes}


def endpoint_slug(method: str, path: str) -> str:
    """Canonical note filename stem, e.g. ('POST','/api/v1/users') -> 'post_api_v1_users'.

    The query string is dropped; the same endpoint therefore maps to one stable note across
    the capture-observed pass and any later target pass.
    """
    only_path = _urlsplit(path or "").path.strip("/")
    base = f"{method}_{only_path}" if only_path else f"{method}_root"
    slug = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    return slug or "endpoint"
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_api_recon_burp_helpers.py -q`
Expected: PASS (5 passed).

- [ ] **Step 6: Commit**

```bash
git add skill_library/api-recon-toolkit/scripts/_burp.py tests/test_api_recon_burp_helpers.py
git commit -m "feat(secassess): port Burp parser + add recon helpers (jwt/slug/asset)"
```

---

### Task 2: `burp.py` capture-inspection CLI

**Files:**
- Create: `skill_library/api-recon-toolkit/scripts/burp.py`
- Test: `tests/test_api_recon_burp_cli.py`

**Interfaces:**
- Consumes: `_burp` (Task 1).
- Produces (used by prompts in Task 5): CLI `python3 burp.py <sub> <xml> [...]` with subcommands `hosts`, `index` (`--apis-only --method --status --url-contains --format`), `view` (`--index N` + `--summary/--req-headers/--resp-headers/--cookies/--req-body/--resp-body/--keys/--limit/--decode-auth`), `harvest` (`--format`). Importable helper `harvest(xml_path) -> dict` for tests.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_recon_burp_cli.py`:
```python
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill_library" / "api-recon-toolkit" / "scripts"
sys.path.insert(0, str(SCRIPTS))
EXAMPLE_XML = str(ROOT / "examples" / "account.vesync.com.xml")

import burp  # noqa: E402


def _run(*args):
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "burp.py"), *args],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_index_apis_only_hides_assets():
    out = _run("index", EXAMPLE_XML, "--apis-only", "--format", "json")
    rows = json.loads(out)
    assert len(rows) == 1
    assert rows[0]["index"] == 22
    assert rows[0]["method"] == "GET"


def test_index_without_filter_lists_all():
    rows = json.loads(_run("index", EXAMPLE_XML, "--format", "json"))
    assert len(rows) == 23


def test_hosts_lists_the_single_domain():
    out = _run("hosts", EXAMPLE_XML)
    assert "account.vesync.com" in out


def test_view_keys_does_not_dump_full_body():
    # response is a 1.2KB HTML doc; --resp-body --limit 200 must truncate
    out = _run("view", EXAMPLE_XML, "--index", "22", "--resp-body", "--limit", "200")
    assert "truncated" in out
    assert "<!doctype html>" in out.lower()


def test_view_decode_auth_finds_jwt_claims_not_raw_token():
    out = _run("view", EXAMPLE_XML, "--index", "22", "--decode-auth")
    assert "22134806" in out          # aud claim surfaced
    assert "HS256" in out             # alg surfaced
    assert "sig_bytes" in out or "signature" in out.lower()


def test_harvest_surfaces_reusable_values():
    data = burp.harvest(EXAMPLE_XML)
    assert "account.vesync.com" in data["hosts"]
    # the account id from the JWT aud claim is captured as a reusable identifier or claim
    blob = json.dumps(data)
    assert "22134806" in blob
    # cookies/headers sections exist (may be empty for this capture) and are lists/dicts
    assert isinstance(data["request_headers"], dict)
    assert isinstance(data["jwts"], list)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_api_recon_burp_cli.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'burp'`.

- [ ] **Step 3: Write `burp.py`**

Create `skill_library/api-recon-toolkit/scripts/burp.py`:
```python
#!/usr/bin/env python3
"""Inspect a Burp Suite XML capture — sliced, never dumped (LLM-friendly).

Subcommands:
    hosts   <xml>                             distinct hosts + counts
    index   <xml> [--apis-only] [--method M] [--status S] [--url-contains T] [--format json]
    view    <xml> --index N [slices...]       one item, header/body slices (truncated)
    harvest <xml> [--format json]             reusable headers/cookies/JWT-claims/identifiers

Examples:
    python3 burp.py index capture.xml --apis-only
    python3 burp.py view capture.xml --index 22 --resp-headers --resp-body --keys
    python3 burp.py view capture.xml --index 22 --decode-auth
    python3 burp.py harvest capture.xml --format json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _burp  # noqa: E402

_SHORT = 200  # header/cookie sample truncation


def _short(v: str, n: int = _SHORT) -> str:
    return v if len(v) <= n else f"{v[:n]}... [+{len(v) - n} chars]"


# --------------------------------------------------------------------- hosts
def cmd_hosts(args) -> int:
    counts = Counter(it.host for it in _burp.iter_items(args.xml))
    for host, n in counts.most_common():
        print(f"{n:>5}  {host}")
    return 0


# --------------------------------------------------------------------- index
def _rows(args):
    for it in _burp.iter_items(args.xml):
        if args.apis_only and _burp.is_asset(it):
            continue
        if args.method and (it.method or "").upper() != args.method.upper():
            continue
        if args.status and args.status not in (it.status or ""):
            continue
        if args.url_contains and args.url_contains.lower() not in (it.url or "").lower():
            continue
        yield {"index": it.index, "method": it.method, "status": it.status,
               "mimetype": it.mimetype, "url": it.url}


def cmd_index(args) -> int:
    rows = list(_rows(args))
    if args.format == "json":
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"{r['index']:>4}  {(r['method'] or '?'):6s} {str(r['status'] or ''):>3}  "
                  f"{(r['mimetype'] or ''):8s}  {r['url']}")
    print(f"\n{len(rows)} item(s)", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------- view
def _find_item(xml_path, index):
    for it in _burp.iter_items(xml_path):
        if it.index == index:
            return it
    raise SystemExit(f"index {index} not found in {xml_path}")


def _print_headers(label, msg):
    print(f"--- {label} headers ({msg.start_line}) ---")
    for k, v in msg.headers:
        print(f"{k}: {_short(v)}")


def _print_cookies(label, msg):
    ck = msg.cookies()
    print(f"--- {label} cookies ({len(ck)}) ---")
    for n, v in ck:
        print(f"  {n} = {_short(v)}")


def _print_body(label, msg, limit, keys_only):
    if not msg.body:
        print(f"--- {label} body: empty ---")
        return
    print(f"--- {label} body ({len(msg.body)} bytes, {msg.content_type() or '?'}) ---")
    if msg.is_json():
        try:
            value = msg.body_json()
        except Exception as e:
            print(f"[json parse failed: {e}]")
            print(_burp.truncate_text(msg.body_text(), limit))
            return
        print(_burp.json_keys_summary(value) if keys_only
              else _burp.truncate_text(json.dumps(value, indent=2, ensure_ascii=False), limit))
        return
    if msg.is_text():
        print(_burp.truncate_text(msg.body_text(), limit))
        return
    print(f"[binary; first {min(len(msg.body), limit)} bytes hex]\n{msg.body[:limit].hex()}")


def _decode_auth(it):
    req = it.request
    sources = []
    auth = req.header("Authorization")
    if auth:
        sources.append(("Authorization header", auth))
    for name, val in req.cookies():
        sources.append((f"cookie {name}", val))
    sources.append(("URL", it.url or ""))
    print("--- decoded auth (claims only; raw tokens NOT shown) ---")
    seen = set()
    any_found = False
    for origin, text in sources:
        for tok in _burp.find_jwts(text):
            if tok in seen:
                continue
            seen.add(tok)
            d = _burp.decode_jwt(tok)
            if not d:
                continue
            any_found = True
            print(f"[{origin}] alg={d['alg']} sig_bytes={d['sig_bytes']}")
            print("  claims: " + json.dumps(d["payload"], ensure_ascii=False))
    if not any_found:
        print("  (no JWTs found in Authorization header, cookies, or URL)")


def cmd_view(args) -> int:
    it = _find_item(args.xml, args.index)
    resp = it.response
    print(f"# {it.method} {it.url}")
    print(f"  status: {it.status}  mime: {it.mimetype}")
    print(f"  request: {len(it.request.headers)} headers, {len(it.request.body)} body bytes")
    print(f"  response: {'none' if resp is None else str(len(resp.headers)) + ' headers, ' + str(len(resp.body)) + ' body bytes'}")
    # Default to a summary if no slice flag was given.
    any_slice = any([args.summary, args.req_headers, args.resp_headers, args.cookies,
                     args.req_body, args.resp_body, args.decode_auth])
    if args.req_headers:
        _print_headers("request", it.request)
    if args.resp_headers and resp is not None:
        _print_headers("response", resp)
    if args.cookies:
        _print_cookies("request", it.request)
        if resp is not None:
            _print_cookies("response", resp)
    if args.req_body:
        _print_body("request", it.request, args.limit, args.keys)
    if args.resp_body and resp is not None:
        _print_body("response", resp, args.limit, args.keys)
    if args.decode_auth:
        _decode_auth(it)
    if not any_slice:
        print("  (pass --req-headers/--resp-headers/--cookies/--req-body/--resp-body/--decode-auth to see more)")
    return 0


# ------------------------------------------------------------------- harvest
_ID_RE = re.compile(r"\b([0-9]{4,}|[0-9a-fA-F]{16,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b")


def harvest(xml_path: str) -> dict:
    hosts: Counter = Counter()
    req_headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    jwts: list[dict] = []
    ident: Counter = Counter()
    seen_tokens: set[str] = set()
    for it in _burp.iter_items(xml_path):
        hosts[it.host] += 1
        if _burp.is_asset(it):
            continue
        req = it.request
        for k, v in req.headers:
            req_headers.setdefault(k, _short(v))
        for n, v in req.cookies():
            cookies.setdefault(n, _short(v))
        for tok in _burp.find_jwts(it.url or "") + _burp.find_jwts(req.header("Authorization") or ""):
            if tok in seen_tokens:
                continue
            seen_tokens.add(tok)
            d = _burp.decode_jwt(tok)
            if d:
                jwts.append({"alg": d["alg"], "sig_bytes": d["sig_bytes"], "claims": d["payload"]})
        for m in _ID_RE.findall((it.url or "") + " " + (req.body_text() if req.is_text() else "")):
            ident[m] += 1
    return {
        "hosts": dict(hosts),
        "request_headers": req_headers,
        "cookies": cookies,
        "jwts": jwts,
        "identifiers": [i for i, _ in ident.most_common(40)],
    }


def cmd_harvest(args) -> int:
    data = harvest(args.xml)
    if args.format == "json":
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    print(f"# hosts\n" + "\n".join(f"  {h} ({n})" for h, n in data["hosts"].items()))
    print(f"\n# reusable request headers ({len(data['request_headers'])})")
    for k, v in data["request_headers"].items():
        print(f"  {k}: {v}")
    print(f"\n# cookies ({len(data['cookies'])})")
    for k, v in data["cookies"].items():
        print(f"  {k} = {v}")
    print(f"\n# decoded JWT/bearer shapes ({len(data['jwts'])}) — claims only, no raw tokens")
    for j in data["jwts"]:
        print(f"  alg={j['alg']} sig_bytes={j['sig_bytes']} claims={json.dumps(j['claims'], ensure_ascii=False)}")
    print(f"\n# candidate identifiers (reusable as required IDs when testing targets)")
    print("  " + ", ".join(data["identifiers"]) if data["identifiers"] else "  (none)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("hosts"); p.add_argument("xml"); p.set_defaults(fn=cmd_hosts)

    p = sub.add_parser("index"); p.add_argument("xml")
    p.add_argument("--apis-only", action="store_true")
    p.add_argument("--method"); p.add_argument("--status"); p.add_argument("--url-contains")
    p.add_argument("--format", choices=["table", "json"], default="table")
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("view"); p.add_argument("xml"); p.add_argument("--index", type=int, required=True)
    p.add_argument("--summary", action="store_true")
    p.add_argument("--req-headers", action="store_true")
    p.add_argument("--resp-headers", action="store_true")
    p.add_argument("--cookies", action="store_true")
    p.add_argument("--req-body", action="store_true")
    p.add_argument("--resp-body", action="store_true")
    p.add_argument("--keys", action="store_true")
    p.add_argument("--decode-auth", action="store_true")
    p.add_argument("--limit", type=int, default=2000)
    p.set_defaults(fn=cmd_view)

    p = sub.add_parser("harvest"); p.add_argument("xml")
    p.add_argument("--format", choices=["table", "json"], default="table")
    p.set_defaults(fn=cmd_harvest)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_api_recon_burp_cli.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add skill_library/api-recon-toolkit/scripts/burp.py tests/test_api_recon_burp_cli.py
git commit -m "feat(secassess): burp.py capture inspector (hosts/index/view/harvest)"
```

---

### Task 3: `targets.py` tolerant targets-file CLI

**Files:**
- Create: `skill_library/api-recon-toolkit/scripts/targets.py`
- Test: `tests/test_api_recon_targets.py`

**Interfaces:**
- Consumes: nothing (standalone; stdlib only).
- Produces (used by prompts in Task 5): `python3 targets.py list <file> [--format json]`, `python3 targets.py show <file> --index N [--part request|response|both] [--limit N]`. Importable: `normalize_jsonish(text) -> str`, `load_targets(path) -> dict` (dict with `domain` and `api` list of `{method,path,request,response}`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_recon_targets.py`:
```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_api_recon_targets.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'targets'`.

- [ ] **Step 3: Write `targets.py`**

Create `skill_library/api-recon-toolkit/scripts/targets.py`:
```python
#!/usr/bin/env python3
"""Inspect the targets scope file — tolerant of hand-authored JSON, sliced output.

Subcommands:
    list <file> [--format json]                 domain + endpoint table (never dumps schemas)
    show <file> --index N [--part request|response|both] [--limit N]   one target's schema slice

The loader tolerates common hand-authoring slips (missing commas between members, trailing
commas, //-line comments, whole-line # comments) so a malformed file does not fail the run.
"""
from __future__ import annotations

import argparse
import json
import re
import sys


def normalize_jsonish(text: str) -> str:
    """Best-effort repair of hand-authored JSON. Only touches structure, never string values."""
    lines = text.split("\n")
    out = []
    for line in lines:
        stripped = line.strip()
        # Drop whole-line comments (# ... or // ...); leaves in-string '#' untouched.
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        out.append(line)
    # Insert a missing comma when a value-closing line is followed by a new key/element.
    for i in range(len(out) - 1):
        cur = out[i].rstrip()
        if not cur:
            continue
        # find the next non-empty line
        j = i + 1
        while j < len(out) and not out[j].strip():
            j += 1
        if j >= len(out):
            break
        nxt = out[j].lstrip()
        ends_value = cur.endswith(('"', "}", "]")) or re.search(r"[0-9eE]$", cur) \
            or cur.endswith(("true", "false", "null"))
        starts_member = nxt.startswith('"') or nxt.startswith("{")
        if ends_value and starts_member and not cur.endswith(","):
            out[i] = cur + ","
    joined = "\n".join(out)
    # Remove trailing commas before a closing bracket/brace.
    joined = re.sub(r",(\s*[}\]])", r"\1", joined)
    return joined


def load_targets(path: str) -> dict:
    raw = open(path, encoding="utf-8").read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        fixed = normalize_jsonish(raw)
        try:
            data = json.loads(fixed)
            print(f"warning: {path} was not strict JSON; loaded after tolerant normalization.",
                  file=sys.stderr)
            return data
        except json.JSONDecodeError as e:
            raise SystemExit(f"could not parse {path} even after normalization: {e}\n"
                             f"--- normalized text ---\n{fixed}")


def _endpoints(data: dict) -> list:
    return data.get("api") or data.get("apis") or data.get("endpoints") or []


def cmd_list(args) -> int:
    data = load_targets(args.file)
    eps = _endpoints(data)
    rows = [{"index": i, "method": e.get("method"), "path": e.get("path"),
             "has_request": bool(e.get("request")), "has_response": bool(e.get("response"))}
            for i, e in enumerate(eps)]
    if args.format == "json":
        print(json.dumps({"domain": data.get("domain"), "count": len(rows), "api": rows}, indent=2))
    else:
        print(f"domain: {data.get('domain')}   ({len(rows)} endpoint(s))")
        for r in rows:
            flags = ("req" if r["has_request"] else "   ") + " " + ("resp" if r["has_response"] else "")
            print(f"{r['index']:>3}  {(r['method'] or '?'):6s} {r['path']}   [{flags}]")
    return 0


def cmd_show(args) -> int:
    data = load_targets(args.file)
    eps = _endpoints(data)
    if not (0 <= args.index < len(eps)):
        raise SystemExit(f"index {args.index} out of range (0..{len(eps) - 1})")
    e = eps[args.index]
    print(f"# [{args.index}] {e.get('method')} {e.get('path')}   (domain: {data.get('domain')})")

    def _emit(label, value):
        value = value or ""
        if args.limit and len(value) > args.limit:
            value = value[:args.limit] + f"\n... [truncated, {len(value) - args.limit} more chars]"
        print(f"\n## {label}\n{value}")

    if args.part in ("request", "both"):
        _emit("request schema", e.get("request"))
    if args.part in ("response", "both"):
        _emit("response schema", e.get("response"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("file")
    p.add_argument("--format", choices=["table", "json"], default="table"); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("show"); p.add_argument("file"); p.add_argument("--index", type=int, required=True)
    p.add_argument("--part", choices=["request", "response", "both"], default="both")
    p.add_argument("--limit", type=int, default=1200); p.set_defaults(fn=cmd_show)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_api_recon_targets.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add skill_library/api-recon-toolkit/scripts/targets.py tests/test_api_recon_targets.py
git commit -m "feat(secassess): targets.py tolerant targets-file inspector (list/show)"
```

---

### Task 4: `vault_note.py` note-filing helper

**Files:**
- Create: `skill_library/api-recon-toolkit/scripts/vault_note.py`
- Test: `tests/test_api_recon_vault_note.py`

**Interfaces:**
- Consumes: `_burp.endpoint_slug` (Task 1).
- Produces (used by prompts in Task 5): `python3 vault_note.py slug "<METHOD> <path>"`; `python3 vault_note.py put (--vault NAME | --root DIR) --domain D --slug S --from FILE [--kind endpoint|recon] [--overwrite]`. Importable: `write_note(root, domain, slug, kind, body, overwrite=True) -> Path`, `resolve_root(vault, runner=...) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_recon_vault_note.py`:
```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_api_recon_vault_note.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'vault_note'`.

- [ ] **Step 3: Write `vault_note.py`**

Create `skill_library/api-recon-toolkit/scripts/vault_note.py`:
```python
#!/usr/bin/env python3
"""File a markdown note into the domain-split assessment vault — no shell-quoting of note bodies.

The agent writes the note body to the workspace with the normal file tool, then files it here:
    endpoint note -> <root>/<domain>/endpoints/<slug>.md
    recon note    -> <root>/<domain>/recon.md

Subcommands:
    slug "<METHOD> <path>"                          print the canonical filename stem
    put (--vault NAME | --root DIR) --domain D --slug S --from FILE [--kind endpoint|recon] [--overwrite]

--vault resolves the vault's on-disk path via `obsidian vault=<NAME> vault info=path`.
--root passes the vault directory directly (used in tests / when the path is already known).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _burp  # noqa: E402


def compute_slug(method_and_path: str) -> str:
    parts = method_and_path.strip().split(None, 1)
    method = parts[0] if parts else "GET"
    path = parts[1] if len(parts) > 1 else "/"
    return _burp.endpoint_slug(method, path)


def _default_runner(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True).stdout


def resolve_root(vault: str, runner=None) -> str:
    runner = runner or _default_runner
    out = runner(["obsidian", f"vault={vault}", "vault", "info=path"])
    # The CLI prints the path (possibly with a label); take the last non-empty line's path-looking token.
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line:
            return line.split("\t")[-1].strip()
    raise SystemExit(f"could not resolve on-disk path for vault '{vault}'")


def write_note(root: str, domain: str, slug: str, kind: str, body: str, overwrite: bool = True) -> Path:
    domain = domain.strip().strip("/")
    if not domain:
        raise SystemExit("domain is required")
    if kind == "recon":
        target = Path(root) / domain / "recon.md"
    else:
        target = Path(root) / domain / "endpoints" / f"{slug}.md"
    if target.exists() and not overwrite:
        raise SystemExit(f"refusing to overwrite existing note: {target} (pass --overwrite)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def cmd_slug(args) -> int:
    print(compute_slug(args.spec))
    return 0


def cmd_put(args) -> int:
    root = args.root or resolve_root(args.vault)
    body = Path(args.from_file).read_text(encoding="utf-8")
    slug = args.slug or "note"
    p = write_note(root, args.domain, slug, args.kind, body, overwrite=args.overwrite)
    print(f"wrote {p}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("slug"); p.add_argument("spec"); p.set_defaults(fn=cmd_slug)
    p = sub.add_parser("put")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--vault"); g.add_argument("--root")
    p.add_argument("--domain", required=True)
    p.add_argument("--slug", default="")
    p.add_argument("--from", dest="from_file", required=True)
    p.add_argument("--kind", choices=["endpoint", "recon"], default="endpoint")
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(fn=cmd_put)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_api_recon_vault_note.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add skill_library/api-recon-toolkit/scripts/vault_note.py tests/test_api_recon_vault_note.py
git commit -m "feat(secassess): vault_note.py domain-split note filer (slug/put)"
```

---

### Task 5: SKILL.md, fixed example, and the workflow YAML

**Files:**
- Create: `skill_library/api-recon-toolkit/SKILL.md`
- Modify: `examples/targets.json` (make it valid, keep it representative)
- Create: `workflows/api-security-assessment.yaml`
- Test: `tests/test_api_security_assessment_workflow.py`

**Interfaces:**
- Consumes: all three CLIs (Tasks 2–4) — referenced by absolute `/mnt/skill_library/...` paths in the prompts.
- Produces: a loadable `WorkflowDef` named `api-security-assessment` with two file inputs and one two-task step.

- [ ] **Step 1: Write the failing workflow-validity test**

Create `tests/test_api_security_assessment_workflow.py`:
```python
from pathlib import Path

import yaml

from atom.workflow.schema import WorkflowDef

WF = Path(__file__).resolve().parents[1] / "workflows" / "api-security-assessment.yaml"


def test_workflow_loads_and_has_expected_shape():
    wf = WorkflowDef.model_validate(yaml.safe_load(WF.read_text()))
    assert wf.name == "api-security-assessment"
    assert wf.notes.enabled is True
    assert wf.notes.vault == "api-security-assessment"
    names = {i.name: i for i in wf.inputs}
    assert names["targets"].type == "file" and names["targets"].required
    assert names["capture"].type == "file" and names["capture"].required
    assert len(wf.steps) == 1
    task_ids = {t.id for t in wf.steps[0].tasks}
    assert task_ids == {"capture_recon", "build_sdk"}
    for t in wf.steps[0].tasks:
        assert t.model == "gemini-pro"          # never gemini-3
        assert "gemini-3" not in (t.model or "")


def test_example_targets_is_valid_json():
    import json
    json.loads((Path(__file__).resolve().parents[1] / "examples" / "targets.json").read_text())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_api_security_assessment_workflow.py -q`
Expected: FAIL — `FileNotFoundError` for the workflow YAML.

- [ ] **Step 3: Fix the example targets file**

Overwrite `examples/targets.json` with valid, representative JSON:
```json
{
  "domain": "my.api.com",
  "api": [
    {
      "method": "POST",
      "path": "/some/api/*/?x={}",
      "request": "{'foo':'int' #optional,'bar':'str' #required}",
      "response": "{'zoo':'int'}"
    },
    {
      "method": "GET",
      "path": "/some/api/user/{id}",
      "request": "{'id':'int' #required}",
      "response": "{'id':'int','email':'str','verified':'bool'}"
    }
  ]
}
```

- [ ] **Step 4: Write the SKILL.md reference**

Create `skill_library/api-recon-toolkit/SKILL.md`:
```markdown
---
name: api-recon-toolkit
description: LLM-friendly CLIs for an authorized API security assessment — slice a Burp XML capture, read a targets scope file, and file notes into a domain-split Obsidian vault. Invoke the scripts by absolute path from bash; they truncate by default so a weak model never loads whole files.
keywords: [burp, api, security, recon, targets, obsidian, jwt, capture]
---

# api-recon-toolkit

Bundled scripts live at `/mnt/skill_library/api-recon-toolkit/scripts/`. All are Python-3 stdlib-only.
Run them from bash with `python3 <abs-path> ...`. Everything truncates by default — pass `--limit` to widen.

## burp.py — inspect a Burp XML capture (slice, never dump)
- `python3 .../burp.py hosts <capture.xml>` — distinct domains + counts.
- `python3 .../burp.py index <capture.xml> --apis-only [--method M --status S --url-contains T] [--format json]` — item table with static assets hidden.
- `python3 .../burp.py view <capture.xml> --index N [--req-headers|--resp-headers|--cookies|--req-body|--resp-body] [--keys] [--limit N] [--decode-auth]` — one item; `--keys` shows JSON key-trees; `--decode-auth` decodes JWTs (claims only, never the raw token).
- `python3 .../burp.py harvest <capture.xml> --format json` — reusable request headers, cookies, decoded JWT/bearer shapes, and candidate identifiers.

## targets.py — read the targets scope file (tolerant loader)
- `python3 .../targets.py list <targets.json> [--format json]` — domain + endpoint table (never dumps schemas).
- `python3 .../targets.py show <targets.json> --index N [--part request|response|both] [--limit N]` — one target's schema slice.

## vault_note.py — file a note into the domain-split vault
- `python3 .../vault_note.py slug "<METHOD> <path>"` — canonical filename stem (e.g. `post_api_v1_users`).
- `python3 .../vault_note.py put --vault api-security-assessment --domain <d> --slug <s> --from <workspace.md> [--kind endpoint|recon] [--overwrite]` — write `<d>/endpoints/<s>.md` (or `<d>/recon.md`). Write the note body with the file tool first; never pass a big note through a shell string.

## Vault shape (split by domain at root)
```
<domain>/recon.md                     reusable values/headers/cookies/IDs/oracles
<domain>/endpoints/<slug>.md          one note per endpoint (observed OR target; same template)
```
```

- [ ] **Step 5: Write the workflow YAML**

Create `workflows/api-security-assessment.yaml`:
```yaml
# workflows/api-security-assessment.yaml — copy to $ATOM_HOME/workflows/ to run it.
# ALSO copy skill_library/api-recon-toolkit/ -> $ATOM_HOME/skill_library/ (ships the CLI tooling),
# and register an Obsidian vault named "api-security-assessment" (Open folder as vault) before running.
name: api-security-assessment
description: Authorized security & privacy assessment of your own API targets — Step 1 sets up recon values, an observed-API inventory, and a target SDK.
notes:
  enabled: true
  vault: api-security-assessment      # notes split by domain at the root; shared across all runs
inputs:
  - name: targets
    type: file
    required: true
    description: JSON file scoping the primary API targets (one domain, up to 25 endpoints).
  - name: capture
    type: file
    required: true
    description: Burp Suite XML capture of live traffic — reusable headers/cookies/IDs + observed-API inventory.
steps:
  - title: Setup
    description: In parallel — harvest reusable values + inventory observed APIs, and build the target SDK.
    tasks:
      - id: capture_recon
        model: gemini-pro
        thinking: high
        prompt: |
          You are doing AUTHORIZED reconnaissance for a security assessment of APIs your org owns.
          Your ONLY input this task is the Burp capture at: {{ capture }}
          Tools live at /mnt/skill_library/api-recon-toolkit/scripts/ (run with python3 <abs path>).
          Work in small steps and NEVER dump whole files — the CLIs slice for you.

          The persistent Obsidian vault is "api-security-assessment"; notes are split by DOMAIN at
          the root: <domain>/recon.md and <domain>/endpoints/<slug>.md.

          STEP A — map the capture:
            python3 /mnt/skill_library/api-recon-toolkit/scripts/burp.py hosts {{ capture }}
            python3 /mnt/skill_library/api-recon-toolkit/scripts/burp.py index {{ capture }} --apis-only --format json

          STEP B — harvest reusable values, once per domain seen:
            python3 /mnt/skill_library/api-recon-toolkit/scripts/burp.py harvest {{ capture }} --format json
          Save the JSON to {{ workspace }}/recon/values.json (use write_file). Then, for EACH domain,
          write a recon note body to {{ workspace }}/recon/<domain>.recon.md with sections:
          Base URLs, Reusable headers, Cookies, Auth/JWT shapes (claims only — NEVER a raw token),
          Candidate identifiers (real IDs reusable as required fields when testing targets), Oracles.
          File it:
            python3 /mnt/skill_library/api-recon-toolkit/scripts/vault_note.py put \
              --vault api-security-assessment --domain <domain> --kind recon \
              --from {{ workspace }}/recon/<domain>.recon.md --overwrite

          STEP C — inventory every observed (non-asset) API. Do them ONE AT A TIME so you never hold
          more than one endpoint in context. For each index from the STEP A list:
            python3 .../burp.py view {{ capture }} --index N --req-headers --resp-headers --cookies
            python3 .../burp.py view {{ capture }} --index N --req-body --resp-body --keys
            python3 .../burp.py view {{ capture }} --index N --decode-auth
          Get the note filename stem:
            python3 .../vault_note.py slug "<METHOD> <path>"
          Write the note body to {{ workspace }}/endpoints/<slug>.md with this exact frontmatter+sections,
          then file it. Template:
            ---
            endpoint: <METHOD> <path>
            domain: <host>
            auth: <scheme, e.g. Bearer JWT (alg=HS256) | session cookie | query authorizeCode | none>
            oracle: <yes|no|unknown>
            source: capture #<index>
            status: observed
            tags: [api-recon]
            ---
            # <METHOD> <path>
            ## Request shape
            ## Response shape
            ## Headers
            ## Auth
            ## Oracle
            <does it confirm the validity/existence of some data? one line + why>
            ## Observations
          File it:
            python3 .../vault_note.py put --vault api-security-assessment --domain <host> \
              --slug <slug> --from {{ workspace }}/endpoints/<slug>.md --overwrite

          STEP D — finish: write {{ outputs }}/recon-summary.md listing each domain, the count of
          endpoints documented, and the most useful reusable identifiers. Then call present_files on
          {{ outputs }}/recon-summary.md. If the capture had no non-asset API items, say so plainly in
          the summary and still record whatever cookies/JWTs harvest found.
      - id: build_sdk
        model: gemini-pro
        thinking: medium
        prompt: |
          You are building a small, well-documented Python SDK for the target APIs of a security
          assessment. Your ONLY input this task is the targets file at: {{ targets }}
          Tools live at /mnt/skill_library/api-recon-toolkit/scripts/ (run with python3 <abs path>).
          Never dump the whole targets file — the CLI slices it for you.

          STEP A — list the scope:
            python3 /mnt/skill_library/api-recon-toolkit/scripts/targets.py list {{ targets }} --format json
          Note the domain and the endpoint indices.

          STEP B — build the SDK in {{ workspace }}/sdk/ (shared, so later steps can import it). Do the
          endpoints ONE AT A TIME to manage context. For each index N:
            python3 .../targets.py show {{ targets }} --index N --part both
          Create/extend {{ workspace }}/sdk/client.py: a documented httpx-based client, one method per
          endpoint, with:
            - a base_url derived from the targets domain and a configurable auth hook (Bearer/cookie
              placeholder — DO NOT hardcode secrets),
            - typed parameters inferred from the request pseudo-schema (mark #required vs #optional),
            - a docstring per method quoting the observed request and response shapes,
            - clear TODO comments where the schema was ambiguous.
          Also write {{ workspace }}/sdk/README.md explaining install (httpx), auth wiring, and one
          usage example per endpoint.

          STEP C — finish: copy the SDK to {{ outputs }}/sdk/ (write_file/bash) and call present_files
          on {{ outputs }}/sdk/client.py and {{ outputs }}/sdk/README.md.
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `python -m pytest tests/test_api_security_assessment_workflow.py -q`
Expected: PASS (2 passed).

- [ ] **Step 7: Commit**

```bash
git add skill_library/api-recon-toolkit/SKILL.md workflows/api-security-assessment.yaml examples/targets.json tests/test_api_security_assessment_workflow.py
git commit -m "feat(secassess): SKILL.md, workflow YAML, valid example targets"
```

---

### Task 6: Full-suite verification, README note, and push

**Files:**
- Modify: `README.md` (add the workflow to the built-in/example list + deploy note)

- [ ] **Step 1: Run the whole test suite (nothing regressed)**

Run: `python -m pytest -q`
Expected: PASS — all prior tests still green plus the ~21 new assertions across the 5 new test files.

- [ ] **Step 2: Smoke-run the CLIs against the real example (manual sanity)**

```bash
S=skill_library/api-recon-toolkit/scripts
python3 $S/burp.py index examples/account.vesync.com.xml --apis-only
python3 $S/burp.py view examples/account.vesync.com.xml --index 22 --decode-auth
python3 $S/targets.py list examples/targets.json
python3 $S/vault_note.py slug "POST /api/v1/users"
```
Expected: index shows 1 item (idx 22); decode-auth prints `aud`=`22134806` + `alg=HS256`; targets list shows 2 endpoints under `my.api.com`; slug prints `post_api_v1_users`.

- [ ] **Step 3: Add a README note (deploy = two artifacts)**

Add to the Workflows/examples section of `README.md`:
```markdown
- **`api-security-assessment`** — authorized API security & privacy assessment (Step 1: recon + SDK)
  on a weak reasoning model (gemini-2.5-pro). Deploy **two** things: copy
  `workflows/api-security-assessment.yaml` → `~/.atom/workflows/`, and
  `skill_library/api-recon-toolkit/` → `~/.atom/skill_library/`. Register an Obsidian vault named
  `api-security-assessment` before running. Notes are split by domain at the vault root.
```

- [ ] **Step 4: Commit and push the branch**

```bash
git add README.md
git commit -m "docs(secassess): document api-security-assessment workflow + deploy steps"
git push -u origin feat/api-security-assessment-workflow
```

---

## Self-Review

**Spec coverage:**
- Two inputs (targets + capture) → Task 5 inputs. ✓
- Scope from targets, live values from capture → prompts in Task 5 (STEP A/B). ✓
- Weak model, not Gemini 3 → Global Constraints + Task 5 test asserts `model == "gemini-pro"`. ✓
- Persistent vault split by domain at root → `write_note` (Task 4) + prompts. ✓
- Step 1 = two parallel tasks (burp recon+values+observed-API docs; SDK build) → Task 5 two-task step. ✓
- Endpoint note contents (request shape, response shape, headers, oracle, observations) → note template in Task 5 prompt + one template reused for observed & target. ✓
- Repeatable parsing via shipped CLI tooling, slice-not-dump, `--limit` → Tasks 2–3 + `--limit` everywhere. ✓
- Up to 25 endpoints, long bodies → sequential one-at-a-time loop in prompts + truncation. ✓
- Ships in repo + pushed to remote; user moves to `~/.atom/` → Task 6 push + README deploy note. ✓
- SDK documented with comments → Task 5 build_sdk prompt. ✓

**Placeholder scan:** No "TBD/TODO-in-plan". (The SDK's own runtime `TODO` comments are an instruction to the model, not a plan gap.) All code steps show complete code.

**Type consistency:** `endpoint_slug(method, path)` defined in `_burp` (Task 1), reused by `vault_note.compute_slug` (Task 4) and asserted identically in both test files. `harvest(xml_path) -> dict` keys (`hosts/request_headers/cookies/jwts/identifiers`) match between Task 2 impl and its test. `write_note(root, domain, slug, kind, body, overwrite)` signature matches Task 4 test. Workflow task ids (`capture_recon`, `build_sdk`) match Task 5 test.

## Execution Handoff

Two execution options:
1. **Subagent-Driven (recommended)** — a fresh subagent per task with review between tasks.
2. **Inline Execution** — execute tasks in this session with checkpoints.
