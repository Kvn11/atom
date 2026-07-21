---
name: api-recon-toolkit
description: LLM-friendly CLIs for an authorized API security assessment — slice a Burp XML capture, read a targets scope file, file notes into a domain-split Obsidian vault, and author/confirm structured findings (JSONL). Invoke the scripts by absolute path from bash; they truncate by default so a weak model never loads whole files.
keywords: [burp, api, security, recon, targets, obsidian, jwt, capture, findings]
---

# api-recon-toolkit

Bundled scripts live at `/mnt/skill_library/api-recon-toolkit/scripts/`. All are Python-3 stdlib-only.
Run them from bash with `python3 <abs-path> ...`. Everything truncates by default — pass `--limit` to widen.

## burp.py — inspect a Burp XML capture (slice, never dump)
- `python3 .../burp.py hosts <capture.xml>` — distinct domains + counts.
- `python3 .../burp.py index <capture.xml> --apis-only [--method M --status S --url-contains T] [--format json]` — item table with static assets hidden.
- `python3 .../burp.py view <capture.xml> --index N [--req-headers|--resp-headers|--cookies|--req-body|--resp-body] [--keys] [--limit N] [--decode-auth]` — one item; `--keys` shows JSON key-trees; `--decode-auth` decodes JWTs (claims only, never the raw token).
- `python3 .../burp.py harvest <capture.xml> --format json` — reusable request headers, cookies, decoded JWT/bearer shapes, and candidate identifiers.
- `python3 .../burp.py identities <capture.xml> --format json` — enumerate the distinct in-scope identities/test accounts (user ids, token shape [redacted], cookie names, source item indices).
- `python3 .../burp.py cred <capture.xml> --index N [--field authorization|cookie|header:NAME]` — print ONE item's RAW credential. Use only inside `TOKEN=$(...)` in a single bash command so the token is never echoed.

## targets.py — read the targets scope file (tolerant loader)
- `python3 .../targets.py list <targets.json> [--format json]` — domain + endpoint table (never dumps schemas).
- `python3 .../targets.py show <targets.json> --index N [--part request|response|both] [--limit N]` — one target's schema slice.

## vault_note.py — file a note into the domain-split vault
- `python3 .../vault_note.py slug "<METHOD> <path>"` — canonical filename stem (e.g. `post_api_v1_users`).
- `python3 .../vault_note.py put --vault api-security-assessment --domain <d> --slug <s> --from <workspace.md> [--kind endpoint|recon] [--overwrite | --if-missing]` — write `<d>/endpoints/<s>.md` (or `<d>/recon.md`). Write the note body with the file tool first; never pass a big note through a shell string. **`--if-missing`** = create only; if the note already exists (e.g. a prior assessment of this domain) it is a no-op (`NOOP:`) and the prior note is preserved — use this for endpoint notes so a re-run never clobbers earlier hypotheses/tests.
- `python3 .../vault_note.py append --vault <v> --domain <d> --slug <s> [--kind endpoint|recon] --from <file>` — append a markdown section (create-if-missing; flock'd). `--kind recon` appends to `<d>/recon.md` (accumulate reusable values across runs); default appends to the endpoint note. Used for `## Hypotheses` and `## Test log` (date-stamp the heading so re-runs stack).
- `python3 .../vault_note.py blocker --vault <v> --domain <d> --id <slug> --endpoint <endpoint-slug> [--desc-from <file>] [--status open|removed]` — create/update `<d>/blockers/BLK-<slug>.md` and append `[[<endpoint-slug>]]` to its Affected endpoints list (deduped, flock'd). Prints `NOOP:` if the endpoint was already linked. Removing a blocker → read its note (or backlinks) for every unblocked endpoint.

## findings.py — author + gate structured findings (JSONL)
A finding: `{"title","description","evidence":[cmd,...],"confirmed":null|true|false}`. Evidence is
**tokenless** — a `TOKEN=$(... burp.py cred ...); curl -H "Authorization: $TOKEN" ...` one-liner
(mint inline, never a literal token).
- `python3 .../findings.py add --from <finding.json> --to <findings.jsonl>` — validate + append (flock'd); **REJECTS a raw JWT**. Write the finding JSON with the file tool first.
- `python3 .../findings.py list <findings.jsonl> [--format json]` — index/title/confirmed/#evidence (never dumps bodies).
- `python3 .../findings.py show <findings.jsonl> --index N` — one full finding (with its evidence commands).
- `python3 .../findings.py confirm --from <raw.jsonl> --index N --to <confirmed.jsonl>` — copy finding N with `confirmed=true`.
- `python3 .../findings.py discard --from <raw.jsonl> --index N --to <discarded.jsonl> --reason "<why>" [--output-from <file>]` — copy with `confirmed=false` + reason + repro_output (JWT-redacted).

## Tool-response signaling (OK / NOOP)
Mutating commands print a leading status: **`OK:`** = something changed; **`NOOP:`** = nothing
changed (the note already existed, or a blocker link was already present). A `NOOP:` means YOUR
WRITE DID NOT HAPPEN — react accordingly (e.g. report "already documented (prior run)"). Errors go
to stderr with a non-zero exit.

## Vault shape (split by domain at root)
```
<domain>/recon.md                     reusable values/headers/cookies/IDs/oracles
<domain>/endpoints/<slug>.md          one note per endpoint (observed OR target; same template)
```
