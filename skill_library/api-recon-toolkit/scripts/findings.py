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
    # split on "\n" only (the exact delimiter append_jsonl writes). str.splitlines() would ALSO
    # break on U+2028/U+2029/U+0085, fragmenting any record whose text contains one and crashing
    # json.loads — those chars legitimately appear in API JSON bodies / LLM-authored descriptions.
    for line in p.read_text(encoding="utf-8").split("\n"):
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
    try:
        obj = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
        finding = validate_finding(obj)      # ValueError (incl. JSONDecodeError) -> clean exit 2
    except ValueError as e:
        print(f"error: invalid finding: {e}", file=sys.stderr)
        return 2
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add"); p.add_argument("--from", dest="from_file", required=True)
    p.add_argument("--to", required=True); p.set_defaults(fn=cmd_add)
    p = sub.add_parser("list"); p.add_argument("jsonl")
    p.add_argument("--format", choices=["text", "json"], default="text"); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("show"); p.add_argument("jsonl")
    p.add_argument("--index", type=int, required=True); p.set_defaults(fn=cmd_show)
    p = sub.add_parser("confirm"); p.add_argument("--from", dest="from_file", required=True)
    p.add_argument("--index", type=int, required=True); p.add_argument("--to", required=True)
    p.set_defaults(fn=cmd_confirm)
    p = sub.add_parser("discard"); p.add_argument("--from", dest="from_file", required=True)
    p.add_argument("--index", type=int, required=True); p.add_argument("--to", required=True)
    p.add_argument("--reason", required=True); p.add_argument("--output-from", dest="output_from")
    p.set_defaults(fn=cmd_discard)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
