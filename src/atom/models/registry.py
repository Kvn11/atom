"""Model registry + factory across Anthropic, OpenAI, Gemini, and Alibaba Qwen.

``init_chat_model`` natively covers anthropic/openai/google_genai; Qwen (DashScope) is not a
recognized provider, so it is built directly via ``langchain_qwq.ChatQwen``. Context window and
capability flags come from the live ``model.profile`` at runtime (see
:mod:`atom.models.profiles`); the static fields here are fallbacks for when a profile is missing
(notably Qwen / brand-new models).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel

Provider = Literal["anthropic", "openai", "google_genai", "qwen"]

DASHSCOPE_INTL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
_DEFAULT_WINDOW = 128_000  # last-resort fallback for unknown "provider:model" strings
DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0  # per-call backstop; middleware owns retry/backoff


@dataclass(frozen=True)
class ModelSpec:
    key: str  # atom-internal short id (config references this)
    provider: Provider
    model_name: str  # id sent to the provider
    init_str: str | None  # "<provider>:<model>" for init_chat_model, or None -> custom factory
    context_window: int  # fallback max input tokens
    max_output_tokens: int
    supports_vision: bool
    supports_reasoning: bool
    api_key_env: str
    base_url: str | None = None


REGISTRY: dict[str, ModelSpec] = {
    # --- Anthropic ---
    "haiku": ModelSpec("haiku", "anthropic", "claude-haiku-4-5", "anthropic:claude-haiku-4-5",
                       200_000, 64_000, True, True, "ANTHROPIC_API_KEY"),
    "sonnet": ModelSpec("sonnet", "anthropic", "claude-sonnet-5", "anthropic:claude-sonnet-5",
                        200_000, 64_000, True, True, "ANTHROPIC_API_KEY"),
    "opus": ModelSpec("opus", "anthropic", "claude-opus-4-8", "anthropic:claude-opus-4-8",
                      200_000, 64_000, True, True, "ANTHROPIC_API_KEY"),
    # --- OpenAI (cheap reasoning default) ---
    "gpt5-mini": ModelSpec("gpt5-mini", "openai", "gpt-5-mini", "openai:gpt-5-mini",
                           400_000, 128_000, True, True, "OPENAI_API_KEY"),
    "o4-mini": ModelSpec("o4-mini", "openai", "o4-mini", "openai:o4-mini",
                         200_000, 100_000, True, True, "OPENAI_API_KEY"),
    # --- Google Gemini ---
    "gemini-pro": ModelSpec("gemini-pro", "google_genai", "gemini-2.5-pro",
                            "google_genai:gemini-2.5-pro", 1_000_000, 65_536, True, True,
                            "GOOGLE_API_KEY"),
    "gemini-flash": ModelSpec("gemini-flash", "google_genai", "gemini-2.5-flash",
                              "google_genai:gemini-2.5-flash", 1_000_000, 65_536, True, True,
                              "GOOGLE_API_KEY"),
    # --- Alibaba Qwen (DashScope) — profile usually absent, so fallbacks matter ---
    "qwen-max": ModelSpec("qwen-max", "qwen", "qwen-max", None, 262_144, 32_768, False, True,
                          "DASHSCOPE_API_KEY", base_url=DASHSCOPE_INTL),
    "qwen-plus": ModelSpec("qwen-plus", "qwen", "qwen-plus", None, 1_000_000, 32_768, False, True,
                           "DASHSCOPE_API_KEY", base_url=DASHSCOPE_INTL),
}


def resolve_spec(key: str) -> ModelSpec:
    """Look up a registry key, or synthesize a spec for a raw ``provider:model`` string.

    This keeps atom configurable: a profile may reference any model, not just the curated set.
    """
    if key in REGISTRY:
        return REGISTRY[key]
    if ":" in key:
        provider, _, model_name = key.partition(":")
        if provider not in ("anthropic", "openai", "google_genai", "google_vertexai"):
            raise KeyError(
                f"Unknown model '{key}'. Add it to atom.models.registry.REGISTRY or use a "
                f"'<provider>:<model>' string with a provider init_chat_model supports."
            )
        env = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(
            provider, "GOOGLE_API_KEY"
        )
        return ModelSpec(key, provider, model_name, key, _DEFAULT_WINDOW, 32_000, True, True, env)
    raise KeyError(f"Unknown model '{key}'. Known keys: {', '.join(REGISTRY)}.")


def clamp_concurrency(n: int) -> int:
    """Clamp a subagent concurrency setting to the mandated [2, 4] band (deviation #9)."""
    return min(4, max(2, int(n)))


# Effort level -> token budget, shared across providers that take a numeric budget.
_EFFORT_BUDGETS = {"minimal": 1024, "low": 4096, "medium": 8192, "high": 24576}


def _coerce_thinking(thinking: Any) -> Any:
    """Normalize a str token budget ("16000") to an int; leave other values as-is."""
    if isinstance(thinking, str) and thinking.strip().lstrip("-").isdigit():
        return int(thinking.strip())
    return thinking


def _thinking_overrides(spec: ModelSpec, thinking: Any) -> dict[str, Any]:
    """Translate a generic ``thinking`` setting into provider-specific kwargs.

    Accepts: None (provider default), "off"/False, "adaptive", an effort str
    ("minimal"/"low"/"medium"/"high"), or an int token budget (or its string form).
    All four providers honor int budgets and effort strings; ``adaptive`` is Anthropic-Opus-only
    and is downgraded to an enabled budget elsewhere.
    """
    if thinking is None:
        return {}
    thinking = _coerce_thinking(thinking)
    off = thinking == "off" or thinking is False

    if spec.provider == "anthropic":
        if off:
            return {}
        if thinking == "adaptive":
            if spec.model_name.startswith("claude-opus"):
                return {"thinking": {"type": "adaptive"}}
            # adaptive is Opus-only; downgrade rather than send an unsupported block.
            return {"thinking": {"type": "enabled", "budget_tokens": _EFFORT_BUDGETS["medium"]}}
        budget = thinking if isinstance(thinking, int) else _EFFORT_BUDGETS.get(thinking, _EFFORT_BUDGETS["medium"])
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}

    if spec.provider == "openai":
        if off:
            return {"reasoning_effort": "minimal"}
        if isinstance(thinking, int):  # OpenAI takes effort levels, not budgets — map to nearest.
            effort = (
                "high" if thinking >= _EFFORT_BUDGETS["high"]
                else "medium" if thinking >= _EFFORT_BUDGETS["medium"]
                else "low"
            )
            return {"reasoning_effort": effort}
        return {"reasoning_effort": thinking if thinking in _EFFORT_BUDGETS else "medium"}

    if spec.provider == "google_genai":
        if off:
            return {"thinking_budget": 0}
        if isinstance(thinking, int):
            return {"thinking_budget": thinking}
        return {"thinking_budget": _EFFORT_BUDGETS.get(thinking, -1)}  # -1 = dynamic (adaptive/unknown)

    if spec.provider == "qwen":
        if off:
            return {"enable_thinking": False}
        out: dict[str, Any] = {"enable_thinking": True}
        budget = thinking if isinstance(thinking, int) else _EFFORT_BUDGETS.get(thinking)
        if budget is not None:
            out["thinking_budget"] = budget
        return out
    return {}


def build_model(key: str, *, thinking: Any = None, **overrides: Any) -> BaseChatModel:
    """Construct a chat model for a registry key (or raw ``provider:model`` string).

    ``max_retries`` is forced to 1 so the provider SDK's own retry layer is disabled and
    ``LLMErrorHandlingMiddleware`` is the single, predictable retry authority across providers
    (Gemini's SDK default is 6). A per-call ``timeout`` backstops a stalled connection.
    """
    spec = resolve_spec(key)
    kwargs = {**_thinking_overrides(spec, thinking), **overrides}
    kwargs.setdefault("max_retries", 1)
    kwargs.setdefault("timeout", DEFAULT_REQUEST_TIMEOUT_SECONDS)
    if spec.init_str is not None:
        from langchain.chat_models import init_chat_model

        return init_chat_model(spec.init_str, **kwargs)
    # Qwen: init_chat_model has no dashscope provider.
    from langchain_qwq import ChatQwen

    kwargs.setdefault("api_base", spec.base_url)
    return ChatQwen(model=spec.model_name, **kwargs)
