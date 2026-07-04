"""Profile-first resolution of context window + capabilities for a built model.

Prefer the live ``model.profile`` (sourced from models.dev, provider-accurate) and fall back
to the static :class:`~atom.models.registry.ModelSpec` fields when a profile is missing — which
is common for Qwen/DashScope and brand-new models.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel

from atom.models.registry import ModelSpec


def _profile(model: BaseChatModel) -> dict[str, Any]:
    prof = getattr(model, "profile", None)
    return prof if isinstance(prof, dict) else {}


def resolve_context_window(model: BaseChatModel, spec: ModelSpec) -> int:
    """Max input tokens for the model — the basis of the 50%-window compaction trigger."""
    return _profile(model).get("max_input_tokens") or spec.context_window


def model_caps(model: BaseChatModel, spec: ModelSpec) -> dict[str, Any]:
    """Capability flags used to gate middleware (vision) and drive compaction."""
    prof = _profile(model)
    return {
        "context_window": prof.get("max_input_tokens") or spec.context_window,
        "max_output_tokens": prof.get("max_output_tokens") or spec.max_output_tokens,
        "supports_vision": bool(prof.get("image_inputs", spec.supports_vision)),
        "supports_reasoning": bool(prof.get("reasoning_output", spec.supports_reasoning)),
        "has_profile": bool(prof),
    }
