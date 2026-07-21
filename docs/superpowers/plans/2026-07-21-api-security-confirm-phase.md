# API Security Confirm Phase + Re-run Safety — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a findings-confirmation phase (Step 4) to `api-security-assessment` — Test emits structured findings to a JSONL, a per-finding sub-agent reproduces each from tokenless curl evidence, confirmed ones become the deliverable — and make the workflow re-run-safe against a previously-assessed domain's persistent vault.

**Architecture:** A new stdlib-only `findings.py` toolkit CLI authors/gates findings (validate + reject raw JWTs + flock'd JSONL append + confirm/discard). `vault_note.py` gains create-if-missing (`put --if-missing`), a recon-targeting `append --kind`, and `OK:`/`NOOP:` tool-response signaling via action-returning helpers. The workflow YAML gains finding emission in Test, re-run-safe writes in Setup/Hypothesize, and a new Confirm step.

**Tech Stack:** Python 3 stdlib (argparse, json, fcntl), pytest, YAML workflows, Obsidian CLI (name-addressed vault). Tests run with `.venv/bin/python -m pytest`.

## Global Constraints

- **Stdlib-only** toolkit scripts (no third-party imports). Run as `python3 <abs-path>`.
- **Secret hygiene:** no raw JWT is ever stored in a finding; `findings.py add` rejects it. `discard` runs `reason`/`repro_output` through `_burp.redact_tokens`.
- **Tokenless evidence:** every `evidence` entry is a self-contained `TOKEN=$(… burp.py cred …); curl -H "Authorization: $TOKEN" …` one-liner (token minted inline, never literal).
- **No-op signaling:** every mutating CLI prints a leading `OK:` (changed) or `NOOP:` (nothing changed); errors go to stderr + non-zero exit.
- **Finding schema (core only):** `title` (non-empty str), `description` (non-empty str), `evidence` (non-empty list of non-empty str), `confirmed` (`null`|`true`|`false`; accept `0`/`1`, default `null`).
- **Coordinator pattern:** lead lists + delegates; one work-item per `subagent_type="bash"` sub-agent; lead never loops over items in its own context.
- **Run tests with** `.venv/bin/python -m pytest` from repo root.
- Toolkit scripts dir: `skill_library/api-recon-toolkit/scripts/`. Test fixtures: `tests/_secassess_fixtures.py` (provides `SAMPLE_JWT`, `write_capture`, `build_capture_xml_multi`).

---

## File Structure

- `skill_library/api-recon-toolkit/scripts/findings.py` — **new.** Finding schema, JWT guard, JSONL IO, subcommands `add`/`list`/`show`/`confirm`/`discard`.
- `skill_library/api-recon-toolkit/scripts/vault_note.py` — **modify.** `write_note` returns `(Path, action)` + `if_missing`; `append_section` gains `kind`; `register_blocker` returns `(Path, action)`; CLIs emit `OK:`/`NOOP:`.
- `skill_library/api-recon-toolkit/SKILL.md` — **modify.** Document `findings.py`, `put --if-missing`, `append --kind`, and the `OK:`/`NOOP:` convention.
- `workflows/api-security-assessment.yaml` — **modify.** Setup/Hypothesize re-run safety; Test emits findings; new Confirm step.
- `tests/test_api_recon_findings.py` — **new.** findings.py unit + CLI tests.
- `tests/test_api_recon_vault_note.py` — **modify.** Unpack `(p, action)` from `write_note`.
- `tests/test_api_recon_vault_ops.py` — **modify.** Unpack `register_blocker`; add `if_missing`/`append --kind recon`/blocker-action tests.
- `tests/test_api_security_assessment_workflow.py` — **modify.** Assert re-run-safe writes, findings emission, and the Confirm step.
- `README.md`, memory `atom-secassess-workflow.md` — **modify.** Documentation.

---

### Task 1: `findings.py` — schema helpers + `add`/`list`/`show`

**Files:**
- Create: `skill_library/api-recon-toolkit/scripts/findings.py`
- Test: `tests/test_api_recon_findings.py`

**Interfaces:**
- Consumes: `_burp.find_jwts(text)->list[str]`.
- Produces: `validate_finding(obj)->dict` (raises `ValueError`), `has_raw_jwt(obj)->str|None`, `read_jsonl(path)->list` (missing→`[]`), `append_jsonl(path, obj)->Path` (flock'd); CLI `add --from F --to J`, `list J [--format json]`, `show J --index N`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_api_recon_findings.py
import json
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / "skill_library" / "api-recon-toolkit" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(TESTS))
import findings  # noqa: E402
import _secassess_fixtures as fx  # noqa: E402


def _cli(*args):
    return subprocess.run([sys.executable, str(SCRIPTS / "findings.py"), *args],
                          capture_output=True, text=True)


def _finding(**over):
    base = {"title": "IDOR reads other user", "description": "returns victim PII",
            "evidence": ["TOKEN=$(x); curl -H \"Authorization: $TOKEN\" https://h/u/2"]}
    base.update(over)
    return base


def test_validate_defaults_confirmed_null():
    out = findings.validate_finding(_finding())
    assert out["confirmed"] is None and out["evidence"]


def test_validate_rejects_bad_fields():
    for bad in [{"title": ""}, {"description": ""}, {"evidence": []}, {"evidence": "x"},
                {"evidence": [""]}]:
        try:
            findings.validate_finding(_finding(**bad))
            assert False, f"expected ValueError for {bad}"
        except ValueError:
            pass


def test_has_raw_jwt_flags_evidence():
    assert findings.has_raw_jwt(_finding(evidence=[f"curl -H 'Authorization: Bearer {fx.SAMPLE_JWT}'"])) == "evidence"
    assert findings.has_raw_jwt(_finding()) is None


def test_read_jsonl_missing_is_empty(tmp_path):
    assert findings.read_jsonl(str(tmp_path / "nope.jsonl")) == []


def test_add_appends_and_defaults(tmp_path):
    fj = tmp_path / "f.json"; jl = tmp_path / "findings.jsonl"
    fj.write_text(json.dumps(_finding()), encoding="utf-8")
    r = _cli("add", "--from", str(fj), "--to", str(jl))
    assert r.returncode == 0 and r.stdout.startswith("OK: added")
    rows = findings.read_jsonl(str(jl))
    assert len(rows) == 1 and rows[0]["confirmed"] is None


def test_add_rejects_raw_jwt(tmp_path):
    fj = tmp_path / "f.json"; jl = tmp_path / "findings.jsonl"
    fj.write_text(json.dumps(_finding(evidence=[f"curl -H 'Authorization: Bearer {fx.SAMPLE_JWT}'"])), encoding="utf-8")
    r = _cli("add", "--from", str(fj), "--to", str(jl))
    assert r.returncode != 0 and "evidence" in r.stderr
    assert findings.read_jsonl(str(jl)) == []


def test_add_flock_no_lost_update(tmp_path):
    jl = tmp_path / "findings.jsonl"
    procs = []
    for i in range(2):
        fj = tmp_path / f"f{i}.json"
        fj.write_text(json.dumps(_finding(title=f"F{i}")), encoding="utf-8")
        procs.append(subprocess.Popen([sys.executable, str(SCRIPTS / "findings.py"),
                                       "add", "--from", str(fj), "--to", str(jl)]))
    assert all(p.wait() == 0 for p in procs)
    assert len(findings.read_jsonl(str(jl))) == 2


def test_list_slices_without_dumping(tmp_path):
    jl = tmp_path / "findings.jsonl"
    findings.append_jsonl(str(jl), findings.validate_finding(_finding(description="SECRET-DESC")))
    r = _cli("list", str(jl))
    assert r.returncode == 0 and "SECRET-DESC" not in r.stdout and "IDOR reads other user" in r.stdout


def test_show_full(tmp_path):
    jl = tmp_path / "findings.jsonl"
    findings.append_jsonl(str(jl), findings.validate_finding(_finding(description="SEEME")))
    r = _cli("show", str(jl), "--index", "0")
    assert r.returncode == 0 and "SEEME" in r.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_api_recon_findings.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'findings'`.

- [ ] **Step 3: Write `findings.py` (helpers + add/list/show)**

```python
#!/usr/bin/env python3
"""Author + gate structured security findings as JSONL for the api-security assessment.

A finding is one JSON object per line:
    {"title": str, "description": str, "evidence": [str, ...], "confirmed": null|true|false}
`evidence` entries are self-contained shell commands that mint their token inline (tokenless) —
no raw JWT is ever stored. `add` refuses any finding containing a raw JWT.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _burp  # noqa: E402


def _norm_confirmed(value):
    if value is None:
        return None
    if value is True or value == 1:
        return True
    if value is False or value == 0:
        return False
    raise ValueError("confirmed must be null, true/1, or false/0")


def validate_finding(obj) -> dict:
    if not isinstance(obj, dict):
        raise ValueError("finding must be a JSON object")
    title = obj.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be a non-empty string")
    desc = obj.get("description")
    if not isinstance(desc, str) or not desc.strip():
        raise ValueError("description must be a non-empty string")
    ev = obj.get("evidence")
    if not isinstance(ev, list) or not ev:
        raise ValueError("evidence must be a non-empty list")
    for i, e in enumerate(ev):
        if not isinstance(e, str) or not e.strip():
            raise ValueError(f"evidence[{i}] must be a non-empty string")
    return {"title": title, "description": desc, "evidence": list(ev),
            "confirmed": _norm_confirmed(obj.get("confirmed"))}


def has_raw_jwt(obj) -> str | None:
    """Return the first field name containing a raw JWT, else None (secret-hygiene guard)."""
    for field in ("title", "description", "evidence"):
        val = obj.get(field)
        for c in (val if isinstance(val, list) else [val]):
            if isinstance(c, str) and _burp.find_jwts(c):
                return field
    return None


def read_jsonl(path: str) -> list:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def append_jsonl(path: str, obj) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:      # O_APPEND + flock: atomic, serialized appends
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    return p


def cmd_add(args) -> int:
    obj = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
    finding = validate_finding(obj)
    bad = has_raw_jwt(finding)
    if bad:
        print(f"error: raw JWT in field '{bad}' — evidence must be tokenless "
              f'(use TOKEN=$(... burp.py cred ...); curl -H "Authorization: $TOKEN" ...)', file=sys.stderr)
        return 2
    print(f"OK: added -> {append_jsonl(args.to, finding)}")
    return 0


def cmd_list(args) -> int:
    rows = read_jsonl(args.jsonl)
    if args.format == "json":
        print(json.dumps([{"index": i, "title": r.get("title"), "confirmed": r.get("confirmed"),
                           "evidence": len(r.get("evidence") or [])} for i, r in enumerate(rows)],
                         ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("(no findings)")
    for i, r in enumerate(rows):
        c = r.get("confirmed")
        mark = "?" if c is None else ("PASS" if c else "FAIL")
        print(f"[{i}] {mark} {r.get('title')}  ({len(r.get('evidence') or [])} evidence)")
    return 0


def cmd_show(args) -> int:
    rows = read_jsonl(args.jsonl)
    if not 0 <= args.index < len(rows):
        print(f"error: index {args.index} out of range (have {len(rows)})", file=sys.stderr)
        return 2
    print(json.dumps(rows[args.index], ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add"); p.add_argument("--from", dest="from_file", required=True)
    p.add_argument("--to", required=True); p.set_defaults(fn=cmd_add)
    p = sub.add_parser("list"); p.add_argument("jsonl")
    p.add_argument("--format", choices=["text", "json"], default="text"); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("show"); p.add_argument("jsonl")
    p.add_argument("--index", type=int, required=True); p.set_defaults(fn=cmd_show)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_api_recon_findings.py -q`
Expected: PASS (all Task-1 tests).

- [ ] **Step 5: Commit**

```bash
git add skill_library/api-recon-toolkit/scripts/findings.py tests/test_api_recon_findings.py
git commit -m "feat(toolkit): findings.py add/list/show with JWT-reject + flock append"
```

---

### Task 2: `findings.py` — `confirm`/`discard`

**Files:**
- Modify: `skill_library/api-recon-toolkit/scripts/findings.py`
- Test: `tests/test_api_recon_findings.py`

**Interfaces:**
- Consumes: `read_jsonl`, `append_jsonl`, `_burp.redact_tokens`.
- Produces: CLI `confirm --from RAW --index N --to CONF`, `discard --from RAW --index N --to DISC --reason "…" [--output-from FILE]`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_api_recon_findings.py
def test_confirm_copies_with_true(tmp_path):
    raw = tmp_path / "raw.jsonl"; conf = tmp_path / "confirmed.jsonl"
    findings.append_jsonl(str(raw), findings.validate_finding(_finding(title="keep me")))
    r = _cli("confirm", "--from", str(raw), "--index", "0", "--to", str(conf))
    assert r.returncode == 0 and r.stdout.startswith("OK: confirmed")
    rows = findings.read_jsonl(str(conf))
    assert rows[0]["confirmed"] is True and rows[0]["title"] == "keep me"


def test_discard_records_reason_and_redacts_output(tmp_path):
    raw = tmp_path / "raw.jsonl"; disc = tmp_path / "discarded.jsonl"
    out = tmp_path / "out.txt"
    out.write_text(f"HTTP/2 200\nleaked {fx.SAMPLE_JWT}\n", encoding="utf-8")
    findings.append_jsonl(str(raw), findings.validate_finding(_finding(title="drop me")))
    r = _cli("discard", "--from", str(raw), "--index", "0", "--to", str(disc),
             "--reason", "403 for attacker; not reproducible", "--output-from", str(out))
    assert r.returncode == 0 and r.stdout.startswith("OK: discarded")
    row = findings.read_jsonl(str(disc))[0]
    assert row["confirmed"] is False and "not reproducible" in row["reason"]
    assert fx.SAMPLE_JWT not in row["repro_output"] and "JWT redacted" in row["repro_output"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_api_recon_findings.py -q -k "confirm or discard"`
Expected: FAIL — `argument cmd: invalid choice: 'confirm'`.

- [ ] **Step 3: Add `confirm`/`discard`**

Add these functions and wire them into `main()` before `args = ap.parse_args()`:

```python
def _pick(rows, index):
    if not 0 <= index < len(rows):
        raise SystemExit(f"index {index} out of range (have {len(rows)})")
    return dict(rows[index])


def cmd_confirm(args) -> int:
    obj = _pick(read_jsonl(args.from_file), args.index)
    obj["confirmed"] = True
    print(f"OK: confirmed F{args.index} -> {append_jsonl(args.to, obj)}")
    return 0


def cmd_discard(args) -> int:
    obj = _pick(read_jsonl(args.from_file), args.index)
    obj["confirmed"] = False
    obj["reason"] = _burp.redact_tokens(args.reason or "")
    if args.output_from:
        obj["repro_output"] = _burp.redact_tokens(Path(args.output_from).read_text(encoding="utf-8"))
    print(f"OK: discarded F{args.index} -> {append_jsonl(args.to, obj)}")
    return 0
```

```python
    p = sub.add_parser("confirm"); p.add_argument("--from", dest="from_file", required=True)
    p.add_argument("--index", type=int, required=True); p.add_argument("--to", required=True)
    p.set_defaults(fn=cmd_confirm)
    p = sub.add_parser("discard"); p.add_argument("--from", dest="from_file", required=True)
    p.add_argument("--index", type=int, required=True); p.add_argument("--to", required=True)
    p.add_argument("--reason", required=True); p.add_argument("--output-from", dest="output_from")
    p.set_defaults(fn=cmd_discard)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_api_recon_findings.py -q`
Expected: PASS (all findings tests).

- [ ] **Step 5: Commit**

```bash
git add skill_library/api-recon-toolkit/scripts/findings.py tests/test_api_recon_findings.py
git commit -m "feat(toolkit): findings.py confirm/discard (discard redacts repro output)"
```

---

### Task 3: `vault_note.py` — `write_note` action + `put --if-missing`

**Files:**
- Modify: `skill_library/api-recon-toolkit/scripts/vault_note.py:50-62,139-145,152-160`
- Test: `tests/test_api_recon_vault_note.py`, `tests/test_api_recon_vault_ops.py`

**Interfaces:**
- Produces: `write_note(root, domain, slug, kind, body, overwrite=True, if_missing=False) -> (Path, action)` where `action ∈ {"wrote","skipped"}`; CLI `put … --if-missing` prints `NOOP: note exists, skipped -> <p>` or `OK: wrote -> <p>`.

- [ ] **Step 1: Update the two existing `write_note` assertions to unpack, and add failing no-op tests**

In `tests/test_api_recon_vault_note.py`, change the two call sites:

```python
def test_write_endpoint_note_creates_nested_path(tmp_path):
    body = "---\nendpoint: GET /\n---\n# GET /\nhello"
    p, action = vault_note.write_note(str(tmp_path), "account.vesync.com", "get_root", "endpoint", body)
    assert action == "wrote"
    assert p == tmp_path / "account.vesync.com" / "endpoints" / "get_root.md"
    assert p.read_text() == body


def test_write_recon_note_goes_to_domain_root(tmp_path):
    p, action = vault_note.write_note(str(tmp_path), "my.api.com", "ignored", "recon", "# recon")
    assert action == "wrote" and p.read_text() == "# recon"
```

Add to `tests/test_api_recon_vault_ops.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_api_recon_vault_note.py tests/test_api_recon_vault_ops.py -q`
Expected: FAIL — `write_note` returns a `Path` (not unpackable to 2), `if_missing` is an unexpected kwarg.

- [ ] **Step 3: Modify `write_note` and `cmd_put`**

Replace `write_note` (lines 50-62):

```python
def write_note(root: str, domain: str, slug: str, kind: str, body: str,
               overwrite: bool = True, if_missing: bool = False) -> tuple[Path, str]:
    domain = domain.strip().strip("/")
    if not domain:
        raise SystemExit("domain is required")
    if kind == "recon":
        target = Path(root) / domain / "recon.md"
    else:
        target = Path(root) / domain / "endpoints" / f"{slug}.md"
    if target.exists():
        if if_missing:
            return target, "skipped"            # no-op: caller reports NOOP, prior note preserved
        if not overwrite:
            raise SystemExit(f"refusing to overwrite existing note: {target} (pass --overwrite)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target, "wrote"
```

Replace `cmd_put` (lines 139-145):

```python
def cmd_put(args) -> int:
    root = args.root or resolve_root(args.vault)
    body = Path(args.from_file).read_text(encoding="utf-8")
    slug = args.slug or "note"
    p, action = write_note(root, args.domain, slug, args.kind, body,
                           overwrite=args.overwrite, if_missing=args.if_missing)
    print(f"NOOP: note exists, skipped -> {p}" if action == "skipped" else f"OK: wrote -> {p}")
    return 0
```

Add the flag to the `put` subparser (after the `--overwrite` line, ~line 159):

```python
    p.add_argument("--if-missing", dest="if_missing", action="store_true")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_api_recon_vault_note.py tests/test_api_recon_vault_ops.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skill_library/api-recon-toolkit/scripts/vault_note.py tests/test_api_recon_vault_note.py tests/test_api_recon_vault_ops.py
git commit -m "feat(toolkit): vault_note put --if-missing no-op + OK/NOOP output"
```

---

### Task 4: `vault_note.py` — `append --kind recon` + `register_blocker` action

**Files:**
- Modify: `skill_library/api-recon-toolkit/scripts/vault_note.py:86-95,98-116,124-136,162-171`
- Test: `tests/test_api_recon_vault_ops.py`

**Interfaces:**
- Produces: `append_section(root, domain, slug, text, kind="endpoint") -> Path` (`kind="recon"` → `<domain>/recon.md`); `register_blocker(...) -> (Path, action)` where `action ∈ {"created","updated","unchanged"}`; CLI `append --kind {endpoint,recon}`; `blocker` prints `NOOP: … already linked …` on `unchanged`.

- [ ] **Step 1: Update existing `register_blocker` call sites to unpack, and add failing tests**

In `tests/test_api_recon_vault_ops.py`, update the three existing `register_blocker` calls that bind the return:

```python
    p, _ = vault_note.register_blocker(str(tmp_path), "api.example.com", "no-second-account",
                                       "post_api_v1_users", description="need a 2nd test account")
```
```python
    p, _ = vault_note.register_blocker(str(tmp_path), "d.com", "waf-403", "ep_b", status="removed")
```
(the un-bound `register_blocker(...)` calls need no change.)

Add new tests:

```python
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
    assert "NOOP:" in blk("ep_a").stdout and "already linked" in blk("ep_a").stdout


def test_append_kind_recon_targets_recon_md(tmp_path):
    p = vault_note.append_section(str(tmp_path), "d.com", "ignored", "## Recon — 2026-07-21\n- x", kind="recon")
    assert p == tmp_path / "d.com" / "recon.md" and "## Recon" in p.read_text()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_api_recon_vault_ops.py -q`
Expected: FAIL — `register_blocker` returns a `Path` (not unpackable), `append_section` has no `kind`.

- [ ] **Step 3: Modify `append_section`, `register_blocker`, `cmd_append`, `cmd_blocker`**

Replace `append_section` (lines 86-95):

```python
def append_section(root: str, domain: str, slug: str, text: str, kind: str = "endpoint") -> Path:
    """Append a markdown section (create-if-missing, flock'd). kind='recon' -> <domain>/recon.md."""
    domain = domain.strip("/")
    if kind == "recon":
        target = Path(root) / domain / "recon.md"
    else:
        target = Path(root) / domain / "endpoints" / f"{slug}.md"

    def _t(old: str) -> str:
        body = old.rstrip("\n")
        sep = "\n\n" if body else ""
        return body + sep + text.rstrip("\n") + "\n"

    return _locked_rmw(target, _t)
```

Replace `register_blocker` (lines 98-116):

```python
def register_blocker(root: str, domain: str, blocker_id: str, endpoint_slug: str,
                     description: str | None = None, status: str | None = None) -> tuple[Path, str]:
    """Create/update <domain>/blockers/BLK-<id>.md; append [[endpoint]] (deduped). Returns (path, action)."""
    path = Path(root) / domain.strip("/") / "blockers" / f"BLK-{blocker_id}.md"
    link = f"- [[{endpoint_slug}]]"
    state = {"action": "unchanged"}

    def _t(old: str) -> str:
        created = not old.strip()
        if created:
            old = (f"---\nid: BLK-{blocker_id}\nstatus: {status or 'open'}\nkind: blocker\n---\n"
                   f"# BLK-{blocker_id}\n\n{description or '(no description)'}\n\n## Affected endpoints\n")
        changed = created
        if status:
            new = re.sub(r"(?m)^status:.*$", f"status: {status}", old, count=1)
            changed = changed or (new != old)
            old = new
        if "## Affected endpoints" not in old:
            old = old.rstrip("\n") + "\n\n## Affected endpoints\n"
            changed = True
        if link not in old:
            old = old.rstrip("\n") + "\n" + link + "\n"
            changed = True
        state["action"] = "created" if created else ("updated" if changed else "unchanged")
        return old

    _locked_rmw(path, _t)
    return path, state["action"]
```

Replace `cmd_append` (lines 124-128) and `cmd_blocker` (lines 131-136):

```python
def cmd_append(args) -> int:
    root = args.root or resolve_root(args.vault)
    text = Path(args.from_file).read_text(encoding="utf-8")
    p = append_section(root, args.domain, args.slug, text, kind=args.kind)
    print(f"OK: appended -> {p}")
    return 0


def cmd_blocker(args) -> int:
    root = args.root or resolve_root(args.vault)
    desc = Path(args.desc_from).read_text(encoding="utf-8") if args.desc_from else None
    p, action = register_blocker(root, args.domain, args.id, args.endpoint, description=desc, status=args.status)
    if action == "unchanged":
        print(f"NOOP: [[{args.endpoint}]] already linked to BLK-{args.id}, no change -> {p}")
    else:
        print(f"OK: blocker {action} -> {p}")
    return 0
```

Add `--kind` to the `append` subparser (line ~164) and make `--slug` non-required:

```python
    p = sub.add_parser("append")
    g = p.add_mutually_exclusive_group(required=True); g.add_argument("--vault"); g.add_argument("--root")
    p.add_argument("--domain", required=True); p.add_argument("--slug", default="")
    p.add_argument("--kind", choices=["endpoint", "recon"], default="endpoint")
    p.add_argument("--from", dest="from_file", required=True); p.set_defaults(fn=cmd_append)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_api_recon_vault_ops.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skill_library/api-recon-toolkit/scripts/vault_note.py tests/test_api_recon_vault_ops.py
git commit -m "feat(toolkit): vault_note append --kind recon + blocker action (created/updated/unchanged)"
```

---

### Task 5: `SKILL.md` — document `findings.py`, `--if-missing`, `--kind`, and `OK:`/`NOOP:`

**Files:**
- Modify: `skill_library/api-recon-toolkit/SKILL.md`

**Interfaces:** Consumes the Task 1-4 CLIs (documentation only).

- [ ] **Step 1: Add a `findings.py` section and update `vault_note.py` docs**

After the `vault_note.py` section, add:

```markdown
## findings.py — author + gate structured findings (JSONL)
A finding: `{"title","description","evidence":[cmd,...],"confirmed":null|true|false}`. Evidence is
tokenless — a `TOKEN=$(... burp.py cred ...); curl -H "Authorization: $TOKEN" ...` one-liner.
- `python3 .../findings.py add --from <finding.json> --to <findings.jsonl>` — validate + append; REJECTS a raw JWT. Write the finding JSON with the file tool first.
- `python3 .../findings.py list <findings.jsonl> [--format json]` — index/title/confirmed/#evidence (never dumps bodies).
- `python3 .../findings.py show <findings.jsonl> --index N` — one full finding (with its evidence commands).
- `python3 .../findings.py confirm --from <raw.jsonl> --index N --to <confirmed.jsonl>` — copy finding N with confirmed=true.
- `python3 .../findings.py discard --from <raw.jsonl> --index N --to <discarded.jsonl> --reason "<why>" [--output-from <file>]` — copy with confirmed=false + reason + repro_output (JWT-redacted).

## Tool-response signaling (OK / NOOP)
Mutating commands print a leading status: `OK:` = something changed; `NOOP:` = nothing changed
(the note already existed, or a blocker link was already present). A `NOOP:` means YOUR WRITE DID
NOT HAPPEN — react accordingly (e.g. report "already documented (prior run)"). Errors go to stderr
with a non-zero exit. New: `vault_note.py put --if-missing` (create-only, no-op if the note exists)
and `vault_note.py append --kind recon` (append to `<domain>/recon.md`).
```

- [ ] **Step 2: Verify the doc lists every subcommand**

Run: `grep -c "findings.py" skill_library/api-recon-toolkit/SKILL.md`
Expected: ≥ 5 (add/list/show/confirm/discard).

- [ ] **Step 3: Commit**

```bash
git add skill_library/api-recon-toolkit/SKILL.md
git commit -m "docs(toolkit): document findings.py + OK/NOOP + if-missing/append-kind in SKILL.md"
```

---

### Task 6: Workflow — Setup + Hypothesize re-run safety

**Files:**
- Modify: `workflows/api-security-assessment.yaml` (Setup `capture_recon` STEP B + STEP C sub-agent step 4; Hypothesize `hypothesize` sub-agent steps 2 & 4)
- Test: `tests/test_api_security_assessment_workflow.py`

**Interfaces:** Consumes `vault_note.py put --if-missing`, `append --kind recon` (Tasks 3-4).

- [ ] **Step 1: Write the failing assertions**

Add to `tests/test_api_security_assessment_workflow.py`:

```python
def test_setup_and_hypothesize_are_rerun_safe():
    tasks = {t.id: t for s in _load().steps for t in s.tasks}
    recon = tasks["capture_recon"].prompt
    hyp = tasks["hypothesize"].prompt
    # endpoint notes are create-if-missing (never re-clobber a prior assessment's note)
    assert "--if-missing" in recon and "--overwrite" not in recon
    assert "--if-missing" in hyp
    # recon.md accumulates a dated section instead of overwriting
    assert "append --kind recon" in recon and "## Recon — {{ date }}" in recon
    # appended endpoint sections are date-stamped so re-assessment stacks, not duplicates
    assert "## Hypotheses — {{ date }}" in hyp
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_api_security_assessment_workflow.py::test_setup_and_hypothesize_are_rerun_safe -q`
Expected: FAIL (prompts still use `--overwrite` / undated headings).

- [ ] **Step 3: Edit the workflow prompts**

In `capture_recon` **STEP B**, replace the recon-note write block so it wraps the body in a dated heading and appends:

```
          STEP B — harvest reusable values (you do this):
            python3 /mnt/skill_library/api-recon-toolkit/scripts/burp.py harvest {{ capture }} --format json
          Save the JSON to {{ workspace }}/recon/values.json (write_file). Then, for EACH domain, write a
          recon section to {{ workspace }}/recon/<domain>.recon.md whose FIRST line is the heading
          "## Recon — {{ date }}" followed by: Base URLs, Reusable headers, Cookies, Auth/JWT shapes
          (claims only — NEVER a raw token), Candidate identifiers, Oracles. APPEND it (the domain may
          already have recon from a prior assessment — appending preserves it):
            python3 /mnt/skill_library/api-recon-toolkit/scripts/vault_note.py append \
              --vault api-security-assessment --domain <domain> --kind recon \
              --from {{ workspace }}/recon/<domain>.recon.md
```

In the `capture_recon` sub-agent prompt **step 4**, change the `put` to create-if-missing and handle the no-op:

```
            4. File the note into the vault (create-if-missing — a prior assessment may already have
               documented this endpoint; do NOT clobber its hypotheses/tests):
                 python3 /mnt/skill_library/api-recon-toolkit/scripts/vault_note.py put \
                   --vault api-security-assessment --domain <HOST> --slug <slug> \
                   --from {{ workspace }}/endpoints/<slug>.md --if-missing
               If it prints "NOOP: note exists" the endpoint was documented in a prior run — that is
               fine; do not retry with --overwrite.
            5. Reply with ONE line: "<METHOD> <PATH> -> oracle=<yes|no|unknown> (new|prior-run)".
```

In the `hypothesize` sub-agent prompt **step 2**, change the stub `put` from `--overwrite` to `--if-missing`:

```
                 python3 /mnt/skill_library/api-recon-toolkit/scripts/vault_note.py put --vault api-security-assessment --domain <HOST> --slug <slug> --from {{ workspace }}/hyp/<slug>.md --if-missing
```

In the `hypothesize` sub-agent prompt **step 4**, date-stamp the section heading:

```
            4. Write the hypotheses to {{ workspace }}/hyp/<slug>.hyp.md (write_file) as:
                 ## Hypotheses — {{ date }}
                 ### H1 — <category> — <one-line theory>
```

- [ ] **Step 4: Run to verify it passes (and no regressions in this file)**

Run: `.venv/bin/python -m pytest tests/test_api_security_assessment_workflow.py -q`
Expected: PASS (new test + all existing).

- [ ] **Step 5: Commit**

```bash
git add workflows/api-security-assessment.yaml tests/test_api_security_assessment_workflow.py
git commit -m "feat(workflow): re-run-safe vault writes in Setup + Hypothesize (if-missing, dated sections)"
```

---

### Task 7: Workflow — Test step emits findings

**Files:**
- Modify: `workflows/api-security-assessment.yaml` (`test` sub-agent prompt step 3 + new emit step; STEP C)
- Test: `tests/test_api_security_assessment_workflow.py`

**Interfaces:** Consumes `findings.py add` (Task 1). Produces `{{ workspace }}/findings.jsonl`.

- [ ] **Step 1: Write the failing assertions**

```python
def test_test_step_emits_findings_jsonl():
    p = _task("test")
    assert "findings.py add" in p
    assert "{{ workspace }}/findings.jsonl" in p
    # emitted evidence is tokenless (mint inline), and the test-log heading is date-stamped
    assert "## Test log — {{ date }}" in p
    assert 'TOKEN=$(' in p and "--field authorization" in p
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_api_security_assessment_workflow.py::test_test_step_emits_findings_jsonl -q`
Expected: FAIL.

- [ ] **Step 3: Edit the `test` sub-agent prompt**

Date-stamp the test-log heading in **step 3** (`## Test log` → `## Test log — {{ date }}`), and insert a new **step 3b** after the test-log append:

```
            3b. For each hypothesis you CONFIRMED (a real, reproduced vulnerability — especially a
                privacy/PII leak), emit a structured finding. Write {{ workspace }}/test/<slug>.<Hn>.finding.json
                (write_file) as:
                  {"title": "<one-line vuln title>",
                   "description": "<what an unauthorized caller obtains and why it is a vuln — PII by field name, never raw values>",
                   "evidence": ["<the EXACT tokenless one-liner that reproduced it: TOKEN=$(python3 /mnt/skill_library/api-recon-toolkit/scripts/burp.py cred {{ capture }} --index <IDENTITY_INDEX> --field authorization); curl -sS -X <METHOD> 'https://<HOST><path>' -H \"Authorization: $TOKEN\" -H 'User-Agent: <UA>' <other headers>>"]}
                Never put a raw token in evidence — mint it inline with $(...). Then append it:
                  python3 /mnt/skill_library/api-recon-toolkit/scripts/findings.py add --from {{ workspace }}/test/<slug>.<Hn>.finding.json --to {{ workspace }}/findings.jsonl
                (findings.py REJECTS a raw JWT — if it errors, fix the evidence to use $(...).)
```

Update the sub-agent reply line (step 5) and STEP C:

```
            5. Reply ONE line: "<METHOD> <PATH> -> confirmed=<n> findings=<n> blocked=<n>".
```
```
          STEP C — report (you do this): after all sub-agents report, write {{ outputs }}/test-report.md —
          confirmed findings by severity (call out privacy/PII leaks), the count of findings emitted to
          {{ workspace }}/findings.jsonl (python3 .../findings.py list {{ workspace }}/findings.jsonl),
          plus a blocker table: for each BLK-*.md under <HOST>/blockers/, its id, status, and the count
          of affected endpoints. Call present_files on {{ outputs }}/test-report.md.
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_api_security_assessment_workflow.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add workflows/api-security-assessment.yaml tests/test_api_security_assessment_workflow.py
git commit -m "feat(workflow): Test step emits tokenless findings to findings.jsonl"
```

---

### Task 8: Workflow — new Confirm step (Step 4)

**Files:**
- Modify: `workflows/api-security-assessment.yaml` (append a 4th step)
- Test: `tests/test_api_security_assessment_workflow.py`

**Interfaces:** Consumes `findings.py list/show/confirm/discard`. Produces `{{ outputs }}/confirmed-findings.jsonl` (deliverable) + `{{ outputs }}/discarded-findings.jsonl`.

- [ ] **Step 1: Write the failing assertions**

```python
def test_has_confirm_step():
    wf = _load()
    assert [s.title for s in wf.steps] == ["Setup", "Hypothesize", "Test", "Confirm"]
    conf = wf.steps[3].tasks
    assert [t.id for t in conf] == ["confirm"]
    assert conf[0].model == "gemini-3.5-flash"


def test_confirm_prompt_reproduces_and_gates():
    p = _task("confirm")
    assert "COORDINATOR" in p and "delegate_task" in p and 'subagent_type="bash"' in p
    assert "findings.py list" in p and "findings.py show" in p
    assert "findings.py confirm" in p and "findings.py discard" in p
    assert "confirmed-findings.jsonl" in p and "discarded-findings.jsonl" in p
    assert "verbatim" in p.lower()                 # evidence commands run exactly as recorded
    assert "0 findings" in p or "nothing to confirm" in p.lower()   # zero-finding graceful path
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_api_security_assessment_workflow.py -q -k confirm`
Expected: FAIL — only 3 steps, no `confirm` task.

- [ ] **Step 3: Append the Confirm step to the YAML**

```yaml
  - title: Confirm
    description: Independently reproduce each emitted finding from its curl evidence — confirmed findings are the deliverable; non-reproducible ones go to an audit log.
    tasks:
      - id: confirm
        model: gemini-3.5-flash
        thinking: high
        prompt: |
          You are the LEAD coordinating INDEPENDENT reproduction of the findings emitted by the Test
          step of an authorized assessment of your org's own APIs. Raw findings:
          {{ workspace }}/findings.jsonl. Capture (identities/tokens): {{ capture }}. Toolkit at
          /mnt/skill_library/api-recon-toolkit/scripts/. Vault: api-security-assessment.

          YOU ARE A COORDINATOR. Do NOT reproduce findings yourself — delegate each to a sub-agent.
          Looping over findings in your own context will hit the recursion limit.

          STEP A — list the findings (you do this):
            python3 /mnt/skill_library/api-recon-toolkit/scripts/findings.py list {{ workspace }}/findings.jsonl --format json
          If there are 0 findings, there is nothing to confirm: create an empty deliverable
          (`: > {{ outputs }}/confirmed-findings.jsonl` via write_file of an empty file), write
          {{ outputs }}/confirmation-summary.md saying "Test emitted 0 findings; nothing to confirm",
          call present_files on both, and STOP (do not delegate).

          SAFETY: reproduction is live but SAFE-BY-DEFAULT. If a finding's evidence is destructive
          /mutating, do NOT re-send it — discard it with reason "destructive — not re-sent".
          ANTI-BOT: mint-once (reuse the captured token; re-mint only on 401), corpus User-Agent,
          throttle <=2 req/s. Copy these rules into every sub-agent prompt.

          STEP B — call delegate_task to run one sub-agent PER finding index (subagent_type="bash",
          REQUIRED — a general-purpose sub-agent has no shell and will fail). You may run several in
          parallel (atom caps concurrency at 4). Give each this EXACT prompt, substituting <N> and
          the ANTI-BOT rules text:

            You are reproducing ONE security finding. Do only this one. Authorized, live, SAFE-BY-DEFAULT.
            Raw findings: {{ workspace }}/findings.jsonl. Anti-bot rules: <RULES>.
            1. Read the finding (title, description, evidence commands):
                 python3 /mnt/skill_library/api-recon-toolkit/scripts/findings.py show {{ workspace }}/findings.jsonl --index <N>
            2. Run EACH evidence command VERBATIM, one per bash call (each is a self-contained
               TOKEN=$(...); curl ... one-liner — the $TOKEN capture only works within a single call).
               Do not edit the commands except to obey the anti-bot rules.
            3. Decide: does the response REPRODUCE the vulnerability in `description`? For a privacy/IDOR
               finding, it reproduces only if the attacker identity actually receives the victim's PII
               fields (record PII by FIELD NAME/presence, never raw values). A 401/403/empty/So-such
               result does NOT reproduce.
            4a. Reproduced -> keep it:
                 python3 /mnt/skill_library/api-recon-toolkit/scripts/findings.py confirm --from {{ workspace }}/findings.jsonl --index <N> --to {{ outputs }}/confirmed-findings.jsonl
            4b. NOT reproduced -> discard it to the audit log. Write a ONE-LINE reason + a minimal
                output snippet (status line + why; NO raw PII) to {{ workspace }}/confirm/<N>.out.txt, then:
                 python3 /mnt/skill_library/api-recon-toolkit/scripts/findings.py discard --from {{ workspace }}/findings.jsonl --index <N> --to {{ outputs }}/discarded-findings.jsonl --reason "<why>" --output-from {{ workspace }}/confirm/<N>.out.txt
            5. Reply ONE line: "F<N> -> confirmed" or "F<N> -> discarded (<why>)".

          STEP C — assemble (you do this): after all sub-agents report,
            python3 /mnt/skill_library/api-recon-toolkit/scripts/findings.py list {{ outputs }}/confirmed-findings.jsonl
          Write {{ outputs }}/confirmation-summary.md: total emitted, how many confirmed, how many
          discarded and their reasons. The file {{ outputs }}/confirmed-findings.jsonl is the FINAL
          DELIVERABLE. Call present_files on {{ outputs }}/confirmed-findings.jsonl and
          {{ outputs }}/confirmation-summary.md.
```

- [ ] **Step 4: Run to verify it passes (whole workflow test file)**

Run: `.venv/bin/python -m pytest tests/test_api_security_assessment_workflow.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add workflows/api-security-assessment.yaml tests/test_api_security_assessment_workflow.py
git commit -m "feat(workflow): Confirm step — per-finding reproduction -> confirmed-findings.jsonl deliverable"
```

---

### Task 9: Docs (README) + memory + full suite

**Files:**
- Modify: `README.md`, `/Users/kev/.claude/projects/-Users-kev-gitclones-atom/memory/atom-secassess-workflow.md`

**Interfaces:** Documentation only.

- [ ] **Step 1: Update `README.md`**

In the api-security-assessment section, add a "Confirm" phase bullet and a "Re-run safety" note: the workflow is now Setup → Hypothesize → Test → Confirm; Test emits `findings.jsonl`; Confirm independently reproduces each finding and writes `confirmed-findings.jsonl` (the deliverable) + `discarded-findings.jsonl` (audit log); re-running over a previously-assessed domain preserves prior notes (create-if-missing) and accumulates `recon.md`.

- [ ] **Step 2: Update the memory file**

Append the confirm-phase + re-run-safety facts to `atom-secassess-workflow.md` (new step, findings schema, `findings.py`, `OK:`/`NOOP:` convention, if-missing/append-kind), keeping the MEMORY.md pointer line accurate.

- [ ] **Step 3: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (all prior tests + the new findings/vault/workflow tests).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: document Confirm phase + re-run safety in README"
```

---

## Self-Review

**1. Spec coverage.** findings.py schema+guard (T1-2) ✓; secret hygiene / JWT reject (T1) ✓; tokenless evidence (T1 guard + T7 prompt) ✓; discard audit log w/ redaction (T2) ✓; `OK:`/`NOOP:` (T3-4 + SKILL T5) ✓; re-run safety — if-missing endpoint notes (T3+T6), accumulating recon.md (T4+T6), date-stamped sections (T6-7) ✓; Test emits findings (T7) ✓; Confirm step incl. zero-finding path (T8) ✓; docs (T5, T9) ✓.

**2. Placeholder scan.** All code steps show full code; prompt edits quote exact replacement text; every test has real assertions. No TBD/TODO.

**3. Type consistency.** `write_note -> (Path, str)` defined T3, consumed by `cmd_put` T3. `register_blocker -> (Path, str)` defined T4, consumed by `cmd_blocker` T4 + tests. `append_section(..., kind=)` defined T4, consumed by T6 recon.md prompt. `findings.py` subcommand names (`add`/`list`/`show`/`confirm`/`discard`) identical across T1/T2/T5/T7/T8. `{{ workspace }}/findings.jsonl` path identical in T7 (write) and T8 (read). Confirmed.
