# Export run — unbounded-size download

**Date:** 2026-07-17
**Status:** Approved, ready for planning
**Area:** `observability/export`, `observability/langfuse_export`, `api/app`, `workflow/run_store`, `atom-ui`

## Problem

The "Export run" feature is supposed to let a user pull a run's observability traces
(LangSmith or LangFuse) and download them. Today it does neither of the size-safe things it
needs to:

1. **Generation buffers the whole export twice in RAM.** All four exporter paths
   (`export_run` / `export_task` × LangSmith / LangFuse) finish with:

   ```python
   tmp.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
   os.replace(tmp, path)
   ```

   `json.dumps(...)` materializes the entire JSON as one Python `str` — a second full-size copy
   on top of the already-in-memory `roots`/`traces` dict, inflated further by `indent=2`. For an
   80 MB+ export that is ~160 MB+ resident at the peak, all avoidable.
   Sites: `export.py:174`, `export.py:251`, `langfuse_export.py:213`, `langfuse_export.py:290`.

2. **There is no download route at all.** `POST /api/runs/{run_id}/export` returns only
   `{path, complete, counts}` — the browser never receives bytes. And the produced file is
   unreachable through any existing route: `GET …/artifacts/{rel}` is path-guarded to
   `artifacts/` (`run_store.py:249-254`), while exports are written to `run_dir/export.json`
   (whole run) and `run_dir/exports/s<step>__<task>.json` (task) — the *parent* of `artifacts/`.
   So a large export can be generated on the server yet has no path to the browser.

The user confirmed the deployment context is **direct / local access** (no reverse proxy, gateway
or CDN in front), so the only real ceiling is server RAM and, on the client, whether the browser
must hold the payload in JS memory. There is no configured 80 MB limit anywhere in the codebase;
the only numeric caps are inbound uploads (`max_file_bytes = 26_214_400`, 25 MiB) and the UI
inline-preview threshold (`MAX_INLINE = 2_000_000`, 2 MB) — neither touches exports.

## Goals

- An export of arbitrary size (well beyond 80 MB) can be **generated** without holding a second
  full-size copy in memory.
- That export can be **downloaded to the browser** without buffering the whole payload in either
  server RAM or JS memory — it streams from disk to the browser, chunked.
- Reuse the patterns already in the codebase (`FileResponse`, `_content_disposition`, `<a download>`
  anchors, atomic tmp-write + `os.replace`). No new dependencies.

## Non-goals (explicitly rejected)

- **gzip / chunked / resumable / range-request transfer.** Direct/local access means no proxy
  size cap; `FileResponse` streaming already removes the only ceiling (server RAM). Compression or
  range machinery would be unused complexity (YAGNI).
- **Per-root streaming fetch** (bounding memory to one trace tree at a time). The completeness
  poll and the remote SDK both inherently materialize the full trace set in memory before it can
  be counted/written, so this would be a large rework of the fetch/poll loop that would not even
  reduce the fetch-time footprint. Eliminating the second (serialized-string) copy is the
  high-value, low-risk win; this is disproportionate.
- **Auto-triggering the download in one click.** The generate step (`POST`) polls the backend for
  up to 30 s, so a programmatic `a.click()` after that `await` is a stale user gesture that some
  browsers treat as a non-user-initiated download and may block. Surfacing an explicit Download
  link (a fresh gesture) is more robust. (Revisit only if the UX proves clunky.)

## Design

### 1. Memory-safe generation

Add one small helper in `observability/export.py` (imported by `langfuse_export.py`, which already
imports `build_envelope`/`expected_root_count` from there):

```python
def _atomic_write_json(path: Path, obj: Any) -> None:
    """Write `obj` as pretty JSON to `path` atomically, streaming the encode.

    `json.dump(obj, fp, ...)` writes incrementally via `iterencode`, so it never builds the whole
    serialized string in memory the way `json.dumps(...)` does — the peak footprint stays at ~one
    copy (the source dict) instead of two. tmp + os.replace keeps the write atomic, matching
    RunStore.save.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(obj, fp, indent=2)
    os.replace(tmp, path)
```

Replace all four `json.dumps(...) + write_text` blocks (`export.py:171-175`, `export.py:248-252`,
`langfuse_export.py:210-214`, `langfuse_export.py:287-291`) with a single
`_atomic_write_json(path, envelope)` call. Output stays **byte-identical** (same `indent=2`), so
existing exporter tests are the regression guard.

Add a path helper to `RunStore` so the whole-run export location is single-sourced between the
writer and the new reader (today `export_run` hardcodes the literal `run_dir / "export.json"`):

```python
def export_path(self, run_id: str) -> Path:
    """Whole-run trace export location (task exports use task_export_path)."""
    return self.run_dir(run_id) / "export.json"
```

Use `store.export_path(run_id)` in `export.py`'s `export_run`. The task path helper
(`task_export_path`) already exists and is reused as-is.

The CLI (`atom workflow export`) calls the same `export_run` / `export_task` functions, so it
inherits the memory fix with no separate change. Writing to disk *is* the download for a CLI user.

### 2. Streamed download route

Add to `api/app.py`:

```
GET /api/runs/{run_id}/export/download                  → run_dir/export.json
GET /api/runs/{run_id}/export/download?step=&task=      → task export (both params required)
```

Handler outline:

```python
@app.get("/api/runs/{run_id}/export/download")
def download_export(run_id: str, step: int | None = None, task: str | None = None):
    try:
        manifest = store.load(run_id)
    except FileNotFoundError:
        raise HTTPException(404, "run not found")
    if step is not None and task is not None:
        s = next((s for s in manifest.steps if s.index == step), None)
        if s is None or not any(t.id == task for t in s.tasks):
            raise HTTPException(404, "task not found")   # also blocks path traversal via `task`
        path = store.task_export_path(run_id, step, task)
        fname = f"atom-export-{run_id}-s{step}-{task}.json"
    else:
        path = store.export_path(run_id)
        fname = f"atom-export-{run_id}.json"
    if not path.is_file():
        raise HTTPException(404, "export not generated yet — POST this run's /export first")
    return FileResponse(
        path, media_type="application/json",
        headers={"Content-Disposition": _content_disposition(fname)},
    )
```

Why this is size-safe: `FileResponse` streams the file from disk in chunks (anyio) and sets
`Content-Length` from `os.stat` — the server never holds the payload in memory. This is the exact
mechanism the artifact download already relies on.

Why it is traversal-safe: for the task scope, `(step, task)` must resolve to a real task in the
manifest before any path is built — mirroring the validation `export_task` already does on write —
so a crafted `task` value (e.g. `../../etc/passwd`) is rejected with a 404 and never reaches
`task_export_path`. The whole-run path is a fixed filename with no user-supplied segment. Scope
rule matches the POST: task scope requires **both** `step` and `task`; anything else is run scope.

`_content_disposition` (the existing RFC 6266 injection-safe builder) is reused; the download
filename is server-controlled and descriptive.

### 3. UI — a real download

`atom-ui/src/RunView.tsx`: after a successful export (`res.fetched_roots > 0`), the existing export
banner gains a **"Download export ↓"** anchor pointing at the new GET route:

```tsx
<a className="export-dl" href={downloadHref} download>Download export ↓</a>
```

- It is a plain `<a href download>` — the browser streams the response straight to disk; JS never
  holds the bytes (unlike `fetch().blob()`, which would defeat the purpose).
- It is a fresh user click (not a stale post-`await` gesture), so no browser download-block risk.
- The partial-export case (`complete === false`) still gets a working download link for the partial
  file; the banner keeps its existing `partial: N/M` note.
- The link's scope matches what was exported: `runExport(body)` already knows run vs task, so the
  href is built from the same `body`.

`atom-ui/src/api.ts`: add an URL builder mirroring `artifactUrl`:

```ts
export const exportDownloadUrl = (id: string, body?: { step: number; task: string }) =>
  body
    ? `/api/runs/${id}/export/download?step=${body.step}&task=${encodeURIComponent(body.task)}`
    : `/api/runs/${id}/export/download`;
```

`runExport` stashes the built href on the success banner state so the anchor can render it.

## Data / control flow

```
User clicks "Export run"/"Export task"
  → POST /api/runs/{id}/export            (generate: poll backend ≤30s, stream-write to disk)
  → 200 {path, complete, fetched_roots, expected_roots}
  → banner shows result + "Download export ↓" anchor (when fetched_roots > 0)
User clicks the anchor
  → GET /api/runs/{id}/export/download[?step&task]
  → FileResponse streams run_dir/export.json (or exports/…) from disk, chunked
  → browser writes bytes straight to the downloads folder (never in JS memory)
```

Generation and delivery stay decoupled: generation persists the file (a real run artifact) and
reports completeness; delivery serves the persisted file. A download never re-fetches from the
backend.

## Testing

Backend (`pytest`):

1. **Regression — identical output.** With the existing exporter fakes, `export_run` /
   `export_task` produce byte-identical `export.json` after the `_atomic_write_json` refactor
   (both providers).
2. **Streaming-path guard.** Monkeypatch `json.dumps` in the exporter module to raise, and assert
   the export still succeeds — locking in that the code uses `json.dump(fp)` and cannot silently
   regress to the double-buffering `json.dumps(...)`.
3. **Download route happy path.** After generating (or writing a fixture) `export.json`, `GET
   …/export/download` returns 200, `Content-Type: application/json`, a `Content-Disposition:
   attachment` header, and a body equal to the file. Same for the `?step=&task=` task variant.
4. **Download route 404s.** Unknown run → 404; unknown/mismatched `(step, task)` → 404;
   export not generated yet → 404 with the "POST …/export first" message.
5. **Traversal rejected.** `?step=0&task=../../etc/passwd` → 404 (task not in manifest), no file
   escape.
6. **Large-file streaming.** Synthesize a multi-MB `export.json` on disk, `GET` the route, and
   assert `Content-Length` equals the file size and the full byte stream is returned — the direct
   proof that download is "not limited by size."

UI: keep the change minimal (one anchor + one URL builder). Confirm whether a JS test harness
exists during planning; if not, rely on the `/verify` end-to-end pass to exercise a real download.

## Files touched

- `src/atom/observability/export.py` — add `_atomic_write_json`; use it in `export_run` /
  `export_task`; use `store.export_path`.
- `src/atom/observability/langfuse_export.py` — use `_atomic_write_json` in `export_run` /
  `export_task`.
- `src/atom/workflow/run_store.py` — add `export_path(run_id)`.
- `src/atom/api/app.py` — add the `GET …/export/download` route.
- `atom-ui/src/api.ts` — add `exportDownloadUrl`.
- `atom-ui/src/RunView.tsx` — render the Download anchor in the export banner.
- Tests under the existing observability / api test modules.
