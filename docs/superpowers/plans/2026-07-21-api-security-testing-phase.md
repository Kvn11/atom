# API Security & Privacy Testing Phase — Implementation Plan (Steps 2 & 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the Hypothesize (Step 2) and Test (Step 3) phases to the `api-security-assessment` workflow: per-target-API fan-out that writes security + privacy hypotheses to endpoint notes first, then live-tests them (safe-by-default) documenting attempts, results, and ID-linked blockers.

**Architecture:** Extend the shipped `api-recon-toolkit` CLIs with an identity roster + raw-credential extractor (`burp.py identities`, `burp.py cred`) and two flock'd vault helpers (`vault_note.py append`, `vault_note.py blocker`), then add two sequential workflow steps whose leads coordinate and delegate one `bash` sub-agent per target endpoint (the proven Step-1 pattern). Blockers are their own notes with a maintained `[[endpoint]]` affected-list, so removing a blocker maps to every unblocked endpoint.

**Tech Stack:** Python 3.12 stdlib (argparse/json/re/fcntl/urllib/base64), pytest, atom workflow YAML, the device `obsidian` CLI (runtime only), curl (runtime HTTP).

## Global Constraints

- **Model:** the two new tasks use `model: gemini-3.5-flash`, `thinking: high`. Sub-agents inherit the task model.
- **Stdlib only** in shipped scripts. `fcntl` is POSIX (macOS/linux target — fine).
- **Fan-out, not loops:** each new step's lead is a COORDINATOR that delegates one `delegate_task subagent_type="bash"` per target endpoint; the lead never inspects/tests an endpoint itself.
- **Hypotheses first:** Step 2 writes `## Hypotheses` to every endpoint note before Step 3 tests. Always include a **Privacy** hypothesis ("can an unauthorized user obtain another user's PII here?").
- **Live, safe-by-default (T1):** execute non-destructive probes live under the anti-bot rules; **document but never send** destructive/mutating probes (mark `destructive-skipped`).
- **Capture is the identity roster (T3):** enumerate in-scope identities from the capture via `burp.py identities`; use them (attacker + victim). Blocker only when the capture lacks a needed identity.
- **Secret hygiene:** the roster and vault notes carry **redacted** token shapes (claims + alg, never the raw token). Raw tokens are obtained only via `burp.py cred` **inside `$(...)`** in a single bash command, so they never print to the model context. Record PII by **field name/presence**, never raw values.
- **Blocker organization:** `<domain>/blockers/BLK-<slug>.md` with frontmatter (id/status) + a maintained `## Affected endpoints` list of `[[endpoint-slug]]`; endpoint test-logs reference `[[BLK-<slug>]]`. Controlled slug vocab: `no-second-account, no-victim-id, auth-expired, waf-403, rate-limited, mfa-required, destructive-skipped, endpoint-unreachable, needs-write-scope`.
- **Parallel-safe notes:** `append`/`blocker` do a `flock`'d read-modify-write so concurrent sub-agents can't lose updates to a shared note.
- **Branch:** work on `feat/api-security-testing-phase` (already checked out). Commit after each task. Run tests with `.venv/bin/python -m pytest`.
- **Tests self-contained:** build synthetic captures in-process (`tests/_secassess_fixtures.py`); never depend on gitignored `examples/`.

---

### Task 1: `burp.py identities` + `cred` (capture identity roster)

**Files:**
- Modify: `skill_library/api-recon-toolkit/scripts/burp.py`
- Modify: `tests/_secassess_fixtures.py` (add a second identity + a multi-identity capture builder)
- Test: `tests/test_api_recon_identities.py`

**Interfaces:**
- Consumes: `_burp` (`iter_items`, `is_asset`, `find_jwts`, `decode_jwt`).
- Produces: `identities(xml_path) -> list[dict]` (each: `label, user_ids, auth{alg,claims}, cookie_names, source_indices, user_agents`); CLI `burp.py identities <xml> [--format json]` and `burp.py cred <xml> --index N [--field authorization|cookie|header:NAME]` (prints the RAW value for `$(...)` capture). Fixtures: `SAMPLE_JWT_2`, `build_capture_xml_multi()`.

- [ ] **Step 1: Extend the fixtures with a second identity**

Add to `tests/_secassess_fixtures.py` (after `SAMPLE_JWT`):
```python
# A second in-scope identity (different audience id) for cross-user/roster tests.
SAMPLE_JWT_2 = _make_jwt({
    "iss": "example.com",
    "aud": "99902222",
    "terminalId": "zzz999yyy888",
    "exp": 1779676117243,
    "iat": 1779675217243,
    "jti": "feedfacecafe",
})
```
And add this builder (after `build_capture_xml`):
```python
def build_capture_xml_multi() -> str:
    """Base capture + one more API item authenticated as a SECOND identity (aud 99902222)."""
    extra = _item(
        "https://api.example.com/api/v1/orders", "api.example.com", "GET", "/api/v1/orders", "",
        "200", "JSON",
        b"GET /api/v1/orders HTTP/1.1\r\nHost: api.example.com\r\n"
        b"Authorization: Bearer " + SAMPLE_JWT_2.encode() + b"\r\n\r\n",
        b'HTTP/2 200 OK\r\nContent-Type: application/json\r\n\r\n{"orders":[]}',
    )
    return build_capture_xml().replace("</items>\n", extra + "</items>\n")
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_api_recon_identities.py`:
```python
import json
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / "skill_library" / "api-recon-toolkit" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))

import burp  # noqa: E402
import _secassess_fixtures as fx  # noqa: E402


def _run(*args):
    proc = subprocess.run([sys.executable, str(SCRIPTS / "burp.py"), *args],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_identities_groups_one_account_by_subject(tmp_path):
    xml = fx.write_capture(tmp_path)              # 1 identity (aud 55501234) across items 2 & 3
    ids = burp.identities(xml)
    assert len(ids) == 1
    ent = ids[0]
    assert "55501234" in ent["user_ids"]
    assert ent["auth"]["alg"] == "HS256"
    assert "session" in ent["cookie_names"]       # from the POST request's Cookie header
    assert sorted(ent["source_indices"]) == [2, 3]
    assert fx.SAMPLE_JWT not in json.dumps(ent)    # redacted: claims only, never the raw token


def test_identities_finds_two_distinct_accounts(tmp_path):
    p = tmp_path / "multi.xml"
    p.write_text(fx.build_capture_xml_multi(), encoding="utf-8")
    ids = burp.identities(str(p))
    keys = {u for ent in ids for u in ent["user_ids"]}
    assert {"55501234", "99902222"} <= keys
    assert len(ids) == 2


def test_cred_prints_raw_authorization_for_dollar_capture(tmp_path):
    xml = fx.write_capture(tmp_path)
    out = _run("cred", xml, "--index", "3", "--field", "authorization")
    # cred is the ONE deliberate raw path (for TOKEN=$(...) use) — it must emit the real token.
    assert fx.SAMPLE_JWT in out
    assert out.startswith("Bearer ")
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_api_recon_identities.py -q`
Expected: FAIL — `AttributeError: module 'burp' has no attribute 'identities'`.

- [ ] **Step 4: Implement `identities` + `cred` in `burp.py`**

Add near the top-level functions of `skill_library/api-recon-toolkit/scripts/burp.py` (after `harvest`):
```python
_ID_CLAIM_KEYS = ("sub", "uid", "user_id", "userId", "aud")


def _identity_key(claims: dict):
    for k in _ID_CLAIM_KEYS:
        v = claims.get(k)
        if v:
            return str(v)
    return None


def identities(xml_path: str) -> list:
    """Enumerate distinct in-scope identities from the capture (T3). Keyed by JWT subject/audience,
    falling back to a session-cookie value. Redacted — claims + alg only, never the raw token."""
    roster: dict = {}
    for it in _burp.iter_items(xml_path):
        if _burp.is_asset(it):
            continue
        req = it.request
        toks = _burp.find_jwts(req.header("Authorization") or "") + _burp.find_jwts(it.url or "")
        key = None
        claims: dict = {}
        alg = None
        for tok in toks:
            d = _burp.decode_jwt(tok)
            if d and _identity_key(d["payload"]):
                key, claims, alg = _identity_key(d["payload"]), d["payload"], d["alg"]
                break
        if key is None:
            ck = req.cookies()
            if not ck:
                continue
            key = "cookie:" + ck[0][1][:12]
        ent = roster.setdefault(key, {
            "label": f"id-{key}"[:48], "user_ids": [], "auth": {"alg": alg, "claims": claims},
            "cookie_names": [], "source_indices": [], "user_agents": [],
        })
        for cid in [str(claims[k]) for k in _ID_CLAIM_KEYS if claims.get(k)]:
            if cid not in ent["user_ids"]:
                ent["user_ids"].append(cid)
        for n, _v in req.cookies():
            if n not in ent["cookie_names"]:
                ent["cookie_names"].append(n)
        if it.index not in ent["source_indices"]:
            ent["source_indices"].append(it.index)
        ua = req.header("User-Agent")
        if ua and ua not in ent["user_agents"]:
            ent["user_agents"].append(ua)
        if claims and not ent["auth"]["claims"]:
            ent["auth"] = {"alg": alg, "claims": claims}
    return list(roster.values())


def cmd_identities(args) -> int:
    data = identities(args.xml)
    if args.format == "json":
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(f"# {len(data)} in-scope identity(ies)")
        for e in data:
            print(f"  {e['label']}: user_ids={e['user_ids']} alg={e['auth']['alg']} "
                  f"cookies={e['cookie_names']} source_items={e['source_indices']}")
    return 0


def cmd_cred(args) -> int:
    """Print the RAW credential value for one item — for `TOKEN=$(burp.py cred ...)` capture only."""
    it = _find_item(args.xml, args.index)
    field = args.field.lower()
    if field == "authorization":
        val = it.request.header("Authorization") or ""
    elif field == "cookie":
        val = it.request.header("Cookie") or ""
    elif field.startswith("header:"):
        val = it.request.header(args.field.split(":", 1)[1]) or ""
    else:
        raise SystemExit("--field must be authorization, cookie, or header:NAME")
    print(val)
    return 0
```
Then register the subparsers inside `main()` (after the `harvest` subparser block, before `args = ap.parse_args()`):
```python
    p = sub.add_parser("identities"); p.add_argument("xml")
    p.add_argument("--format", choices=["table", "json"], default="table")
    p.set_defaults(fn=cmd_identities)

    p = sub.add_parser("cred"); p.add_argument("xml"); p.add_argument("--index", type=int, required=True)
    p.add_argument("--field", default="authorization")
    p.set_defaults(fn=cmd_cred)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_api_recon_identities.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add skill_library/api-recon-toolkit/scripts/burp.py tests/_secassess_fixtures.py tests/test_api_recon_identities.py
git commit -m "feat(secassess): burp.py identities roster + cred raw-extractor"
```

---

### Task 2: `vault_note.py append` + `blocker` (flock'd note ops)

**Files:**
- Modify: `skill_library/api-recon-toolkit/scripts/vault_note.py`
- Test: `tests/test_api_recon_vault_ops.py`

**Interfaces:**
- Consumes: `resolve_root` (existing).
- Produces: `append_section(root, domain, slug, text) -> Path`; `register_blocker(root, domain, blocker_id, endpoint_slug, description=None, status=None) -> Path`; CLI `vault_note.py append (--vault N|--root R) --domain D --slug S --from FILE`; `vault_note.py blocker (--vault N|--root R) --domain D --id SLUG --endpoint ENDPOINT_SLUG [--desc-from FILE] [--status open|removed]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_recon_vault_ops.py`:
```python
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / "skill_library" / "api-recon-toolkit" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import vault_note  # noqa: E402


def test_append_creates_then_appends(tmp_path):
    p1 = vault_note.append_section(str(tmp_path), "api.example.com", "get_root", "## Hypotheses\n- H1")
    assert p1 == tmp_path / "api.example.com" / "endpoints" / "get_root.md"
    assert "## Hypotheses" in p1.read_text()
    p2 = vault_note.append_section(str(tmp_path), "api.example.com", "get_root", "## Test log\n- ok")
    body = p2.read_text()
    assert "## Hypotheses" in body and "## Test log" in body      # first section preserved
    assert body.index("## Hypotheses") < body.index("## Test log")


def test_register_blocker_creates_and_dedupes(tmp_path):
    p = vault_note.register_blocker(str(tmp_path), "api.example.com", "no-second-account",
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
    p = vault_note.register_blocker(str(tmp_path), "d.com", "waf-403", "ep_b", status="removed")
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_api_recon_vault_ops.py -q`
Expected: FAIL — `AttributeError: module 'vault_note' has no attribute 'append_section'`.

- [ ] **Step 3: Implement `append_section` + `register_blocker` in `vault_note.py`**

Add `import fcntl` and `import re` to the imports at the top of `skill_library/api-recon-toolkit/scripts/vault_note.py`, then add these functions (after `write_note`):
```python
def _locked_rmw(path: Path, transform) -> Path:
    """Read-modify-write ``path`` under an exclusive flock (create-if-missing). ``transform(old)->new``.

    Uses r+ (NOT a+) because POSIX append mode forces writes to EOF regardless of seek, which would
    corrupt a truncate-rewrite. flock serializes concurrent writers across processes/sub-agents.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    with open(path, "r+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            new = transform(fh.read())
            fh.seek(0)
            fh.truncate()
            fh.write(new)
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    return path


def append_section(root: str, domain: str, slug: str, text: str) -> Path:
    """Append a markdown section to an endpoint note (create-if-missing), without shell-mangling."""
    target = Path(root) / domain.strip("/") / "endpoints" / f"{slug}.md"

    def _t(old: str) -> str:
        body = old.rstrip("\n")
        sep = "\n\n" if body else ""
        return body + sep + text.rstrip("\n") + "\n"

    return _locked_rmw(target, _t)


def register_blocker(root: str, domain: str, blocker_id: str, endpoint_slug: str,
                     description: str | None = None, status: str | None = None) -> Path:
    """Create/update <domain>/blockers/BLK-<id>.md and append [[endpoint]] to its affected list (deduped)."""
    path = Path(root) / domain.strip("/") / "blockers" / f"BLK-{blocker_id}.md"
    link = f"- [[{endpoint_slug}]]"

    def _t(old: str) -> str:
        if not old.strip():
            old = (f"---\nid: BLK-{blocker_id}\nstatus: {status or 'open'}\nkind: blocker\n---\n"
                   f"# BLK-{blocker_id}\n\n{description or '(no description)'}\n\n## Affected endpoints\n")
        if status:  # flip status when a human/agent marks it removed
            old = re.sub(r"(?m)^status:.*$", f"status: {status}", old, count=1)
        if "## Affected endpoints" not in old:
            old = old.rstrip("\n") + "\n\n## Affected endpoints\n"
        if link not in old:
            old = old.rstrip("\n") + "\n" + link + "\n"
        return old

    return _locked_rmw(path, _t)


def cmd_append(args) -> int:
    root = args.root or resolve_root(args.vault)
    text = Path(args.from_file).read_text(encoding="utf-8")
    print(f"appended -> {append_section(root, args.domain, args.slug, text)}")
    return 0


def cmd_blocker(args) -> int:
    root = args.root or resolve_root(args.vault)
    desc = Path(args.desc_from).read_text(encoding="utf-8") if args.desc_from else None
    p = register_blocker(root, args.domain, args.id, args.endpoint, description=desc, status=args.status)
    print(f"blocker -> {p}")
    return 0
```
Register the subparsers inside `main()` (after the `put` block, before `args = ap.parse_args()`):
```python
    p = sub.add_parser("append")
    g = p.add_mutually_exclusive_group(required=True); g.add_argument("--vault"); g.add_argument("--root")
    p.add_argument("--domain", required=True); p.add_argument("--slug", required=True)
    p.add_argument("--from", dest="from_file", required=True); p.set_defaults(fn=cmd_append)

    p = sub.add_parser("blocker")
    g = p.add_mutually_exclusive_group(required=True); g.add_argument("--vault"); g.add_argument("--root")
    p.add_argument("--domain", required=True); p.add_argument("--id", required=True)
    p.add_argument("--endpoint", required=True); p.add_argument("--desc-from", dest="desc_from")
    p.add_argument("--status", choices=["open", "removed"]); p.set_defaults(fn=cmd_blocker)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_api_recon_vault_ops.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add skill_library/api-recon-toolkit/scripts/vault_note.py tests/test_api_recon_vault_ops.py
git commit -m "feat(secassess): vault_note.py append + blocker (flock'd, ID-linked blockers)"
```

---

### Task 3: Workflow Steps 2 & 3 + SKILL.md + shape test

**Files:**
- Modify: `workflows/api-security-assessment.yaml` (append two steps)
- Modify: `skill_library/api-recon-toolkit/SKILL.md` (document identities/cred/append/blocker)
- Test: `tests/test_api_security_assessment_workflow.py` (extend)

**Interfaces:**
- Consumes: all CLIs from Tasks 1–2 (by absolute `/mnt/skill_library/...` path in prompts) + the Step-1 SDK/recon.
- Produces: a 3-step `WorkflowDef` with tasks `capture_recon`+`build_sdk` (Step 1), `hypothesize` (Step 2), `test` (Step 3).

- [ ] **Step 1: Extend the workflow-shape test (failing)**

Append to `tests/test_api_security_assessment_workflow.py`:
```python
def test_has_hypothesize_and_test_steps():
    wf = _load()
    assert [s.title for s in wf.steps] == ["Setup", "Hypothesize", "Test"]
    hyp = wf.steps[1].tasks
    tst = wf.steps[2].tasks
    assert [t.id for t in hyp] == ["hypothesize"]
    assert [t.id for t in tst] == ["test"]
    assert hyp[0].model == "gemini-3.5-flash" and tst[0].model == "gemini-3.5-flash"


def test_hypothesize_prompt_delegates_and_covers_privacy():
    p = {t.id: t for s in _load().steps for t in s.tasks}["hypothesize"].prompt
    assert "COORDINATOR" in p and "delegate_task" in p and 'subagent_type="bash"' in p
    assert "## Hypotheses" in p
    assert "PII" in p and "privacy" in p.lower()
    assert "vault_note.py append" in p


def test_test_prompt_is_safe_by_default_with_antibot_and_blockers():
    p = {t.id: t for s in _load().steps for t in s.tasks}["test"].prompt
    assert "COORDINATOR" in p and "delegate_task" in p and 'subagent_type="bash"' in p
    assert "destructive-skipped" in p and "safe-by-default" in p.lower()
    assert "burp.py identities" in p          # capture is the identity roster (T3)
    assert "burp.py cred" in p and "$(" in p  # raw token only via $(...) capture
    assert "mint-once" in p.lower()           # anti-bot rules present
    assert "vault_note.py blocker" in p and "[[BLK-" in p
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_api_security_assessment_workflow.py -q`
Expected: FAIL — `AssertionError` on the step titles (still only `["Setup"]`).

- [ ] **Step 3: Append Steps 2 & 3 to the workflow YAML**

Append the following two step blocks to `workflows/api-security-assessment.yaml` (keep them under the existing top-level `steps:` list, after the `Setup` step — same indentation as the `- title: Setup` item):
```yaml
  - title: Hypothesize
    description: Fan out per target API — develop security + privacy hypotheses and write them to each endpoint note FIRST.
    tasks:
      - id: hypothesize
        model: gemini-3.5-flash
        thinking: high
        prompt: |
          You are the LEAD coordinating security + privacy HYPOTHESIS development for an authorized
          assessment of your org's own API targets. Input: targets file {{ targets }}. Reusable context:
          the capture {{ capture }} and {{ workspace }}/recon/values.json. Toolkit at
          /mnt/skill_library/api-recon-toolkit/scripts/. Vault: api-security-assessment (notes split by
          DOMAIN at the root: <domain>/endpoints/<slug>.md).

          YOU ARE A COORDINATOR. Do NOT analyze endpoints yourself — that is SUB-AGENT work and looping
          over many endpoints will hit the recursion limit. Your job: list, delegate, summarize.

          STEP A — list the targets (you do this):
            python3 /mnt/skill_library/api-recon-toolkit/scripts/targets.py list {{ targets }} --format json
          Keep the domain (call it <HOST>) and the endpoint indices.

          STEP B — DELEGATE one sub-agent PER target endpoint. For EACH index, call delegate_task with
          subagent_type="bash" (REQUIRED — a general-purpose sub-agent has no shell and will fail). You
          may issue several together to run in parallel. Give each sub-agent this EXACT prompt,
          substituting <INDEX> and <HOST>:

            You are developing security + privacy hypotheses for ONE target API endpoint. Do only this one.
            Targets: {{ targets }}. Reusable values: {{ workspace }}/recon/values.json.
            Toolkit: /mnt/skill_library/api-recon-toolkit/scripts/. Vault: api-security-assessment. Domain: <HOST>.
            1. Read the endpoint shape and compute its note stem:
                 python3 /mnt/skill_library/api-recon-toolkit/scripts/targets.py show {{ targets }} --index <INDEX> --part both
               Take <METHOD> and <PATH> from that output, then:
                 python3 /mnt/skill_library/api-recon-toolkit/scripts/vault_note.py slug "<METHOD> <PATH>"
            2. Ensure the endpoint note exists. Try to read it:
                 obsidian vault=api-security-assessment read file="<HOST>/endpoints/<slug>.md"
               If it does NOT exist, create a stub: write {{ workspace }}/hyp/<slug>.md (write_file) with
               frontmatter (endpoint: <METHOD> <PATH> / domain: <HOST> / auth: unknown / oracle: unknown /
               status: target / tags: [api-recon]) plus "## Request shape" and "## Response shape" from
               step 1, then file it:
                 python3 /mnt/skill_library/api-recon-toolkit/scripts/vault_note.py put --vault api-security-assessment --domain <HOST> --slug <slug> --from {{ workspace }}/hyp/<slug>.md --overwrite
            3. Develop hypotheses from the endpoint shape + {{ workspace }}/recon/values.json (reusable IDs
               and other identities). Cover each category with one hypothesis where plausible (else write
               "N/A — reason"): IDOR/BOLA, mass-assignment, broken authn/authz, injection
               (SQLi/XSS/SSRF/path/command), and ALWAYS a PRIVACY hypothesis — "can an unauthorized user
               obtain another user's PII via this endpoint?".
            4. Write the hypotheses to {{ workspace }}/hyp/<slug>.hyp.md (write_file) as:
                 ## Hypotheses
                 ### H1 — <category> — <one-line theory>
                 - Probe: <concrete request: method, path, which identity, key params/body>
                 - Privacy? <yes/no>
                 ### H2 — ...
               Then append it to the note (preserves prior sections):
                 python3 /mnt/skill_library/api-recon-toolkit/scripts/vault_note.py append --vault api-security-assessment --domain <HOST> --slug <slug> --from {{ workspace }}/hyp/<slug>.hyp.md
            5. Reply ONE line: "<METHOD> <PATH> -> <N> hypotheses (privacy: yes/no)".

          STEP C — summarize (you do this): after all sub-agents report, write
          {{ outputs }}/hypotheses-summary.md (endpoints theorized, total hypotheses, how many flag
          privacy) and call present_files on it.
  - title: Test
    description: Fan out per target API — live-test the hypotheses (safe-by-default), documenting attempts, results, and ID-linked blockers.
    tasks:
      - id: test
        model: gemini-3.5-flash
        thinking: high
        prompt: |
          You are the LEAD coordinating AUTHORIZED live testing of your org's own API targets.
          Targets: {{ targets }}. Capture: {{ capture }}. SDK: {{ workspace }}/sdk/. Reusable values:
          {{ workspace }}/recon/values.json. Toolkit at /mnt/skill_library/api-recon-toolkit/scripts/.
          Vault: api-security-assessment.

          YOU ARE A COORDINATOR. Do NOT test endpoints yourself — delegate each to a sub-agent.

          SAFETY (safe-by-default): only NON-DESTRUCTIVE probes may be sent (reads / non-mutating). For
          any state-changing/destructive hypothesis (DELETE, account/data mutation, password/email
          change), DOCUMENT the exact probe but DO NOT SEND it — mark it destructive-skipped.

          STEP A — build the identity roster (you do this): the capture holds all in-scope identities/test
          accounts. Enumerate them:
            python3 /mnt/skill_library/api-recon-toolkit/scripts/burp.py identities {{ capture }} --format json
          Note each identity's user id, source item index, cookie names, and user-agent; and the corpus
          User-Agent/header style from {{ workspace }}/recon/values.json. You will pass the relevant
          identities to each sub-agent (an ATTACKER identity + a VICTIM user id for cross-user/privacy tests).

          ANTI-BOT RULES — copy these into EVERY sub-agent prompt: mint-once (reuse a captured token;
          re-mint only on a 401, never pre-emptively); use a corpus User-Agent, never curl/python
          defaults; send the full captured header set; throttle to <=2 req/s per endpoint with 50-200ms
          jitter; use captured identifiers, never enumerate id spaces; on a 403/500/"inactive"/"No active
          account" lockout signal, STOP and record a blocker.

          STEP B — DELEGATE one sub-agent PER target endpoint (subagent_type="bash", REQUIRED). You may
          run several in parallel (atom caps concurrency at 4). Give each this EXACT prompt, substituting
          <INDEX>/<HOST>, the ATTACKER identity's source item index (<IDENTITY_INDEX>), a VICTIM user id,
          the corpus User-Agent, and the ANTI-BOT RULES text:

            You are testing the hypotheses for ONE target API endpoint. Do only this one. Authorized, live,
            SAFE-BY-DEFAULT — never send destructive/mutating requests (document them as destructive-skipped).
            Targets: {{ targets }}. Capture: {{ capture }}. SDK: {{ workspace }}/sdk/. Vault: api-security-assessment.
            Domain: <HOST>. Attacker identity source item: <IDENTITY_INDEX>. Victim user id: <VICTIM_ID>.
            Corpus User-Agent: <UA>. Anti-bot rules: <RULES>.
            1. Compute the slug and read the hypotheses:
                 python3 /mnt/skill_library/api-recon-toolkit/scripts/targets.py show {{ targets }} --index <INDEX> --part both
                 python3 /mnt/skill_library/api-recon-toolkit/scripts/vault_note.py slug "<METHOD> <PATH>"
                 obsidian vault=api-security-assessment read file="<HOST>/endpoints/<slug>.md"
            2. For EACH hypothesis:
               - Destructive/mutating? Do NOT send. Note it as destructive-skipped, reference
                 [[BLK-destructive-skipped]], and register that blocker.
               - Otherwise test it. Authenticate WITHOUT printing the token — capture it in the SAME
                 command with $(...):
                   TOKEN=$(python3 /mnt/skill_library/api-recon-toolkit/scripts/burp.py cred {{ capture }} --index <IDENTITY_INDEX> --field authorization); \
                   curl -sS -X <METHOD> "https://<HOST><path>" -H "Authorization: $TOKEN" -H "User-Agent: <UA>" <other captured headers> <data>
                 For a PRIVACY/IDOR test, authenticate as the ATTACKER and request the VICTIM's id; a 2xx
                 that returns the victim's PII fields = CONFIRMED privacy leak. Record PII by FIELD
                 NAME/presence, never raw values.
            3. Write results to {{ workspace }}/test/<slug>.log.md (write_file):
                 ## Test log
                 ### H1 — <confirmed | not-vulnerable | inconclusive | blocked>
                 - Attempt: <method path, identity used, UA>
                 - Result: <status + minimal evidence (PII as field names)>
                 - Blocker (if any): [[BLK-<slug>]] — <one line>
               Append it: python3 /mnt/skill_library/api-recon-toolkit/scripts/vault_note.py append --vault api-security-assessment --domain <HOST> --slug <slug> --from {{ workspace }}/test/<slug>.log.md
            4. For every blocker you hit, register it (idempotent). Write a one-line description to
               {{ workspace }}/test/<slug>.blk.md, then:
                 python3 /mnt/skill_library/api-recon-toolkit/scripts/vault_note.py blocker --vault api-security-assessment --domain <HOST> --id <blocker-slug> --endpoint <slug> --desc-from {{ workspace }}/test/<slug>.blk.md
               Blocker slugs (prefer these): no-second-account, no-victim-id, auth-expired, waf-403,
               rate-limited, mfa-required, destructive-skipped, endpoint-unreachable, needs-write-scope.
            5. Reply ONE line: "<METHOD> <PATH> -> confirmed=<n> blocked=<n>".

          STEP C — report (you do this): after all sub-agents report, write {{ outputs }}/test-report.md —
          confirmed findings by severity (call out privacy/PII leaks), plus a blocker table: for each
          BLK-*.md under <HOST>/blockers/, its id, status, and the count of affected endpoints. Call
          present_files on {{ outputs }}/test-report.md.
```

- [ ] **Step 4: Document the new commands in SKILL.md**

In `skill_library/api-recon-toolkit/SKILL.md`, under the `burp.py` section add:
```markdown
- `python3 .../burp.py identities <capture.xml> --format json` — enumerate the distinct in-scope identities/test accounts (user ids, token shape [redacted], cookie names, source item indices).
- `python3 .../burp.py cred <capture.xml> --index N [--field authorization|cookie|header:NAME]` — print ONE item's RAW credential. Use only inside `TOKEN=$(...)` in a single bash command so the token is never echoed.
```
and under the `vault_note.py` section add:
```markdown
- `python3 .../vault_note.py append --vault <v> --domain <d> --slug <s> --from <file>` — append a markdown section to an endpoint note (create-if-missing; flock'd). Used for `## Hypotheses` and `## Test log`.
- `python3 .../vault_note.py blocker --vault <v> --domain <d> --id <slug> --endpoint <endpoint-slug> [--desc-from <file>] [--status open|removed]` — create/update `<d>/blockers/BLK-<slug>.md` and append `[[<endpoint-slug>]]` to its Affected endpoints list (deduped, flock'd). Removing a blocker → read its note (or backlinks) for every unblocked endpoint.
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_api_security_assessment_workflow.py -q`
Expected: PASS (all shape + prompt assertions green).

- [ ] **Step 6: Commit**

```bash
git add workflows/api-security-assessment.yaml skill_library/api-recon-toolkit/SKILL.md tests/test_api_security_assessment_workflow.py
git commit -m "feat(secassess): add Hypothesize + Test steps (per-API fan-out, safe-by-default)"
```

---

### Task 4: Full-suite verification, README, real-CLI smoke

**Files:**
- Modify: `README.md` (extend the api-security-assessment paragraph)

- [ ] **Step 1: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — all prior tests plus the ~11 new assertions across the 3 new/extended test files.

- [ ] **Step 2: Smoke-test the new CLIs against the real local example**

```bash
S=skill_library/api-recon-toolkit/scripts
.venv/bin/python $S/burp.py identities examples/account.vesync.com.xml --format json
T=$(.venv/bin/python $S/burp.py cred examples/account.vesync.com.xml --index 22 --field authorization); echo "cred captured ${#T} chars into a var (not shown)"
.venv/bin/python $S/vault_note.py blocker --root /tmp/blk-smoke --domain account.vesync.com --id no-victim-id --endpoint get_root && cat /tmp/blk-smoke/account.vesync.com/blockers/BLK-no-victim-id.md
```
Expected: `identities` lists one identity (aud `22134806`) with redacted claims (no raw JWT); the `cred` var length prints without exposing the token; the blocker note contains `- [[get_root]]`.

- [ ] **Step 3: Extend the README paragraph**

In `README.md`, in the `### API security assessment` section, after the sentence about the SDK, add:
```markdown
Two further steps then run in the same pipeline: **Hypothesize** fans out per target API to write
security + privacy (PII-for-unauthorized-user) hypotheses into each endpoint note first, and **Test**
fans out per target API to live-test them **safe-by-default** (non-destructive only; destructive probes
are documented, never sent) using the capture's in-scope identities. Blockers are their own notes
(`<domain>/blockers/BLK-<slug>.md`) with an affected-endpoints list, so clearing a blocker maps to every
endpoint it unblocked.
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs(secassess): document Hypothesize + Test phases"
```

---

## Self-Review

**Spec coverage:**
- Step 2 Hypothesize, per-API fan-out, hypotheses-first, privacy category → Task 3 hypothesize prompt + tests. ✓
- Step 3 Test, per-API fan-out, live safe-by-default (T1) → Task 3 test prompt + tests. ✓
- Capture = identity roster (T3) → Task 1 `burp.py identities` + Task 3 STEP A. ✓
- Extend same workflow (T2) → Task 3 appends steps to `api-security-assessment.yaml`. ✓
- Blocker notes + `[[endpoint]]` affected-list + `[[BLK-slug]]` refs + controlled vocab → Task 2 `register_blocker` + Task 3 prompt. ✓
- Document attempts/results/blockers → `## Test log` template + `append`/`blocker`. ✓
- Anti-bot rules + mint-once → Task 3 test prompt (§6 rules). ✓
- Secret hygiene (redacted roster/notes; raw only via `$()` cred) → Task 1 `identities` redacted + `cred`; Task 3 `$()` pattern; asserted in tests. ✓
- flock parallel-safe blockers → Task 2 `_locked_rmw` + concurrency test. ✓

**Placeholder scan:** No plan-level TBD/TODO. The `<INDEX>/<HOST>/<VICTIM_ID>` tokens are literal substitution slots the lead fills at runtime (documented as such), not plan gaps. All code steps show complete code.

**Type consistency:** `identities(xml)->list[dict]` keys (`label/user_ids/auth/cookie_names/source_indices/user_agents`) match between Task 1 impl and its test. `append_section(root,domain,slug,text)` and `register_blocker(root,domain,blocker_id,endpoint_slug,description=,status=)` signatures match Task 2 impl and tests. Blocker path `<domain>/blockers/BLK-<id>.md` consistent across impl, test, and the Task 3 prompt. New task ids `hypothesize`/`test` consistent between YAML and the shape test.

## Execution Handoff

Two execution options:
1. **Subagent-Driven (recommended)** — a fresh subagent per task, review between tasks.
2. **Inline Execution** — execute tasks in this session with checkpoints.
