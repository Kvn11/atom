"""_build_context wires the per-run uploads dir into the WorkspaceContext."""
from __future__ import annotations

from atom.runtime import _build_context

_CAPS = {"supports_vision": False}


def test_build_context_sets_uploads_path(base_config):
    ctx = _build_context(
        base_config, user_id="u", thread_id="t", profile_name="default",
        home="/tmp/h", workspace="new", uploads="/runs/r1/uploads",
        caps=_CAPS, window=1000,
    )
    assert ctx["uploads_path"].endswith("/runs/r1/uploads")


def test_build_context_uploads_none_when_absent(base_config):
    ctx = _build_context(
        base_config, user_id="u", thread_id="t", profile_name="default",
        home="/tmp/h", workspace="new", uploads=None,
        caps=_CAPS, window=1000,
    )
    assert ctx["uploads_path"] is None
