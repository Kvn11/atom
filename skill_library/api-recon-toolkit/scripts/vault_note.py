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
import fcntl
import re
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


def cmd_slug(args) -> int:
    print(compute_slug(args.spec))
    return 0


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

    p = sub.add_parser("append")
    g = p.add_mutually_exclusive_group(required=True); g.add_argument("--vault"); g.add_argument("--root")
    p.add_argument("--domain", required=True); p.add_argument("--slug", required=True)
    p.add_argument("--from", dest="from_file", required=True); p.set_defaults(fn=cmd_append)

    p = sub.add_parser("blocker")
    g = p.add_mutually_exclusive_group(required=True); g.add_argument("--vault"); g.add_argument("--root")
    p.add_argument("--domain", required=True); p.add_argument("--id", required=True)
    p.add_argument("--endpoint", required=True); p.add_argument("--desc-from", dest="desc_from")
    p.add_argument("--status", choices=["open", "removed"]); p.set_defaults(fn=cmd_blocker)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
