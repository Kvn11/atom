"""Pure helpers for workflow file-input uploads: safe naming + limit checks.

No I/O — callers (the API, the CLI, RunStore.save_upload) do the reading/writing and use these
to derive the on-disk name and enforce limits. The on-disk name is derived from the (unique)
workflow input NAME, so it is deterministic and collision-free by construction: a caller can
compute the stored path before writing and it is guaranteed to match what save_upload writes.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

from atom.sandbox.paths import VIRTUAL_UPLOADS


class UploadTooLarge(ValueError):
    """Raised when an uploaded file exceeds the configured size limit."""


class UploadTypeNotAllowed(ValueError):
    """Raised when an uploaded file's extension is not in the configured allowlist."""


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _basename(filename: str) -> str:
    """Final path component, treating both / and \\ as separators (client-supplied names)."""
    name = str(filename or "").strip().replace("\\", "/")
    return PurePosixPath(name).name


def safe_extension(original_filename: str) -> str:
    """The sanitized, lowercased suffix (incl. dot) of ``original_filename``, or '' if none."""
    ext = PurePosixPath(_basename(original_filename)).suffix.lower()
    ext = _SAFE.sub("", ext).strip(".")
    return f".{ext}" if ext else ""


def _sanitize_stem(input_name: str) -> str:
    """Filesystem-safe stem for an input name. Injective: sanitization is lossy (distinct raw
    names can reduce to the same safe stem), so whenever it changes the name we append a short
    deterministic hash of the ORIGINAL name. This keeps clean identifiers clean (stem == raw ->
    no suffix) while guaranteeing distinct input names never collide on disk."""
    raw = str(input_name or "").strip()
    stem = _SAFE.sub("-", raw).strip("-.") or "upload"
    if stem != raw:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
        stem = f"{stem}-{digest}"
    return stem


def stored_name(input_name: str, original_filename: str) -> str:
    """Deterministic on-disk name: ``<sanitized input name><sanitized original extension>``."""
    return _sanitize_stem(input_name) + safe_extension(original_filename)


def virtual_upload_path(input_name: str, original_filename: str) -> str:
    """The virtual mount path an agent sees, e.g. /mnt/user-data/uploads/doc.pdf."""
    return f"{VIRTUAL_UPLOADS}/{stored_name(input_name, original_filename)}"


def check_size(nbytes: int, limit: int) -> None:
    if limit and nbytes > limit:
        raise UploadTooLarge(f"file is {nbytes} bytes; limit is {limit}")


def check_extension(original_filename: str, allowed: list[str]) -> None:
    if not allowed:
        return
    ext = safe_extension(original_filename).lstrip(".")
    allow = {a.lower().lstrip(".") for a in allowed}
    if ext not in allow:
        raise UploadTypeNotAllowed(
            f"file type '.{ext or '(none)'}' not allowed; allowed: {', '.join(sorted(allow))}"
        )
