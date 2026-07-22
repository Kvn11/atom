"""Model registry + factory across Anthropic, OpenAI, Gemini, and Alibaba Qwen.

``init_chat_model`` natively covers anthropic/openai/google_genai; Qwen (DashScope) is not a
recognized provider, so it is built directly via ``langchain_qwq.ChatQwen``. Context window and
capability flags come from the live ``model.profile`` at runtime (see
:mod:`atom.models.profiles`); the static fields here are fallbacks for when a profile is missing
(notably Qwen / brand-new models).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel

Provider = Literal["anthropic", "openai", "google_genai", "qwen", "bedrock"]

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
    wire: Literal["anthropic", "openai"] | None = None  # bedrock-only: Bifrost endpoint + client class


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
    # Gemini 3.5 Flash — reasoning-capable; uses the thinking_level enum, not thinking_budget.
    "gemini-3.5-flash": ModelSpec("gemini-3.5-flash", "google_genai", "gemini-3.5-flash",
                                  "google_genai:gemini-3.5-flash", 1_000_000, 65_536, True, True,
                                  "GOOGLE_API_KEY"),
    # --- Alibaba Qwen (DashScope) — profile usually absent, so fallbacks matter ---
    "qwen-max": ModelSpec("qwen-max", "qwen", "qwen-max", None, 262_144, 32_768, False, True,
                          "DASHSCOPE_API_KEY", base_url=DASHSCOPE_INTL),
    "qwen-plus": ModelSpec("qwen-plus", "qwen", "qwen-plus", None, 1_000_000, 32_768, False, True,
                           "DASHSCOPE_API_KEY", base_url=DASHSCOPE_INTL),
    # --- AWS Bedrock via the Bifrost gateway ---
    # base URL + key come from env (ATOM_BIFROST_BASE_URL / ATOM_BIFROST_API_KEY); wire selects the
    # Bifrost drop-in endpoint + LangChain class; model_name is the bare bedrock-runtime id (the
    # "bedrock/" routing prefix is added in build_model). init_str=None -> custom factory branch.
    "bedrock-opus": ModelSpec("bedrock-opus", "bedrock", "us.anthropic.claude-opus-4-8", None,
                              1_000_000, 128_000, True, True, "ATOM_BIFROST_API_KEY", wire="anthropic"),
    "bedrock-sonnet": ModelSpec("bedrock-sonnet", "bedrock", "us.anthropic.claude-sonnet-5", None,
                                1_000_000, 128_000, True, True, "ATOM_BIFROST_API_KEY", wire="anthropic"),
    "bedrock-haiku": ModelSpec("bedrock-haiku", "bedrock",
                               "anthropic.claude-haiku-4-5-20251001-v1:0", None,
                               200_000, 64_000, True, True, "ATOM_BIFROST_API_KEY", wire="anthropic"),
    "bedrock-qwen-coder": ModelSpec("bedrock-qwen-coder", "bedrock",
                                    "qwen.qwen3-coder-480b-a35b-v1:0", None,
                                    131_072, 16_384, False, False, "ATOM_BIFROST_API_KEY", wire="openai"),
    "bedrock-qwen": ModelSpec("bedrock-qwen", "bedrock", "qwen.qwen3-235b-a22b-2507-v1:0", None,
                              262_144, 8_192, False, True, "ATOM_BIFROST_API_KEY", wire="openai"),
    "bedrock-kimi-thinking": ModelSpec("bedrock-kimi-thinking", "bedrock",
                                       "moonshot.kimi-k2-thinking", None,
                                       262_144, 16_384, False, True, "ATOM_BIFROST_API_KEY", wire="openai"),
    "bedrock-kimi": ModelSpec("bedrock-kimi", "bedrock", "moonshotai.kimi-k2.5", None,
                              262_144, 16_384, True, False, "ATOM_BIFROST_API_KEY", wire="openai"),
    "bedrock-gpt-oss": ModelSpec("bedrock-gpt-oss", "bedrock", "openai.gpt-oss-120b-1:0", None,
                                 131_072, 16_384, False, True, "ATOM_BIFROST_API_KEY", wire="openai"),
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

_GEMINI_VER_RE = re.compile(r"gemini-(\d+)")


def _is_gemini_3_plus(model_name: str) -> bool:
    """True for Gemini 3.x+ models, which use the ``thinking_level`` enum instead of an int budget."""
    m = _GEMINI_VER_RE.search(model_name or "")
    return bool(m) and int(m.group(1)) >= 3


def _budget_to_level(budget: int) -> str:
    """Map an int thinking budget to the nearest Gemini-3 ``thinking_level`` bucket."""
    if budget >= _EFFORT_BUDGETS["high"]:
        return "high"
    if budget >= _EFFORT_BUDGETS["medium"]:
        return "medium"
    if budget >= _EFFORT_BUDGETS["low"]:
        return "low"
    return "minimal"


def _coerce_thinking(thinking: Any) -> Any:
    """Normalize a str token budget ("16000") to an int; leave other values as-is."""
    if isinstance(thinking, str) and thinking.strip().lstrip("-").isdigit():
        return int(thinking.strip())
    return thinking


def _anthropic_thinking(spec: ModelSpec, thinking: Any, off: bool) -> dict[str, Any]:
    """Anthropic-style thinking kwargs, shared by direct-Anthropic and Bedrock-Anthropic-wire.

    ``adaptive`` is Opus-only; detected by substring so Bedrock ids (``us.anthropic.claude-opus-4-8``)
    match as well as the bare direct-Anthropic ids (``claude-opus-4-8``).
    """
    if off:
        return {}
    if thinking == "adaptive":
        if "claude-opus" in spec.model_name:
            return {"thinking": {"type": "adaptive"}}
        return {"thinking": {"type": "enabled", "budget_tokens": _EFFORT_BUDGETS["medium"]}}
    budget = thinking if isinstance(thinking, int) else _EFFORT_BUDGETS.get(thinking, _EFFORT_BUDGETS["medium"])
    return {"thinking": {"type": "enabled", "budget_tokens": budget}}


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
        return _anthropic_thinking(spec, thinking, off)

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
        # Gemini 3+ replaced the integer thinking_budget with a thinking_level enum
        # (minimal/low/medium/high); thinking_budget is deprecated for those models.
        if _is_gemini_3_plus(spec.model_name):
            if off:
                return {"thinking_level": "minimal"}  # 3+ cannot fully disable thinking; floor it
            if isinstance(thinking, int):
                return {"thinking_level": _budget_to_level(thinking)}
            return {"thinking_level": thinking if thinking in _EFFORT_BUDGETS else "medium"}
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

    if spec.provider == "bedrock":
        if spec.wire == "anthropic":
            return _anthropic_thinking(spec, thinking, off)
        # openai-wire: Bifrost maps OpenAI-style reasoning -> Bedrock thinkingConfig (best-effort).
        if off or not spec.supports_reasoning:
            return {}
        budget = thinking if isinstance(thinking, int) else _EFFORT_BUDGETS.get(thinking, _EFFORT_BUDGETS["medium"])
        return {"extra_body": {"reasoning": {"max_tokens": max(budget, 1024)}}}
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
