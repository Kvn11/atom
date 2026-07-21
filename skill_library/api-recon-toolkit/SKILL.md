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
- `python3 .../burp.py identities <capture.xml> --format json` — enumerate the distinct in-scope identities/test accounts (user ids, token shape [redacted], cookie names, source item indices).
- `python3 .../burp.py cred <capture.xml> --index N [--field authorization|cookie|header:NAME]` — print ONE item's RAW credential. Use only inside `TOKEN=$(...)` in a single bash command so the token is never echoed.

## targets.py — read the targets scope file (tolerant loader)
- `python3 .../targets.py list <targets.json> [--format json]` — domain + endpoint table (never dumps schemas).
- `python3 .../targets.py show <targets.json> --index N [--part request|response|both] [--limit N]` — one target's schema slice.

## vault_note.py — file a note into the domain-split vault
- `python3 .../vault_note.py slug "<METHOD> <path>"` — canonical filename stem (e.g. `post_api_v1_users`).
- `python3 .../vault_note.py put --vault api-security-assessment --domain <d> --slug <s> --from <workspace.md> [--kind endpoint|recon] [--overwrite]` — write `<d>/endpoints/<s>.md` (or `<d>/recon.md`). Write the note body with the file tool first; never pass a big note through a shell string.
- `python3 .../vault_note.py append --vault <v> --domain <d> --slug <s> --from <file>` — append a markdown section to an endpoint note (create-if-missing; flock'd). Used for `## Hypotheses` and `## Test log`.
- `python3 .../vault_note.py blocker --vault <v> --domain <d> --id <slug> --endpoint <endpoint-slug> [--desc-from <file>] [--status open|removed]` — create/update `<d>/blockers/BLK-<slug>.md` and append `[[<endpoint-slug>]]` to its Affected endpoints list (deduped, flock'd). Removing a blocker → read its note (or backlinks) for every unblocked endpoint.

## Vault shape (split by domain at root)
```
<domain>/recon.md                     reusable values/headers/cookies/IDs/oracles
<domain>/endpoints/<slug>.md          one note per endpoint (observed OR target; same template)
```
