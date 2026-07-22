# AWS Bedrock models via the Bifrost gateway — Design

**Date:** 2026-07-22
**Status:** Approved (brainstorming), pending spec review
**Author:** Kevin (with Claude)

## Summary

Add support for AWS Bedrock–hosted models (Anthropic Claude, Qwen, Moonshot Kimi, and
OpenAI open-weight `gpt-oss`) to atom, routed through a self-hosted **Bifrost** gateway
addressed by a custom domain base URL.

The integration speaks Bifrost's **OpenAI-compatible and Anthropic-compatible drop-in
endpoints** — *not* the native AWS Bedrock API. Consequently it reuses the already-installed
`langchain-openai` and `langchain-anthropic` clients and requires **no new dependencies**
(no `langchain-aws`, no `boto3`). All AWS credentials live server-side inside Bifrost; atom
sends only a Bifrost virtual key.

The change is **additive** and almost entirely localized to
`src/atom/models/registry.py`. The engine, agent, workflow, config-schema, and CLI layers are
untouched, because a Bedrock model resolves through the same
`resolve_spec → build_model` path as every existing model.

## Decisions (locked during brainstorming)

1. **Routing scope — additive.** New `bedrock-*` model entries live alongside the existing
   direct Anthropic/OpenAI/Google/Qwen entries. Both paths coexist; nothing is retired.
2. **Client shape — native split.** Claude models use `ChatAnthropic` against Bifrost's
   `/anthropic` endpoint (native `thinking` blocks that atom's middleware + streaming rely on,
   pass end-to-end). Qwen / Moonshot / gpt-oss use `ChatOpenAI` against Bifrost's `/openai`
   endpoint.
3. **Config surface — gateway in env, models in registry.** The Bifrost base URL and virtual
   key come from environment variables; the model catalog is added to `registry.py` like the
   existing providers. No YAML schema change.
4. **Model list — curated set** (~8 models; one or two per family). Easy to extend later.
5. **Base URL — gateway root.** The configured URL is the Bifrost root (e.g.
   `https://bifrost.example.com`); atom appends the `/openai` or `/anthropic` suffix itself per
   model family.

## Background: how Bifrost is reached (research findings)

- Bifrost exposes drop-in surfaces at path suffixes off the gateway root: OpenAI-compatible at
  `<root>/openai` (SDK posts to `<root>/openai/v1/chat/completions`) and Anthropic-compatible at
  `<root>/anthropic` (SDK posts to `<root>/anthropic/v1/messages`).
- A Bedrock model is selected by putting a **`bedrock/<modelId>`** string in the request `model`
  field. Bifrost translates the OpenAI/Anthropic request shape into Bedrock's **Converse /
  ToolConfig / thinkingConfig** API server-side, and performs AWS SigV4 signing itself.
- **Auth:** a Bifrost **virtual key** carried in the `x-bf-vk` header (works uniformly on both
  `/openai` and `/anthropic`). The client sends **no AWS credentials**.
- **LangChain rule:** use the explicit `ChatOpenAI` / `ChatAnthropic` classes with an overridden
  `base_url`. **Do not** use `init_chat_model("bedrock/...")` — that dispatches to LangChain's
  native boto3/SigV4 Bedrock provider and bypasses the gateway entirely.
- **Converse-only reachability:** Bifrost fronts Bedrock via the `bedrock-runtime` **Converse**
  API. Models served only on `bedrock-mantle` (the OpenAI/Responses surface) have no Converse
  path and are therefore **unreachable** through Bifrost. This excludes the entire **GPT-5.x**
  family and Anthropic's **Mythos** variants. The reachable OpenAI-family models are the
  open-weight **`gpt-oss-*`** models. Because Bifrost uses Converse, the model string is the
  **`bedrock-runtime` model ID** (not the mantle `-instruct`/unversioned variant).

> All exact model IDs, cross-region inference-profile prefixes, and the `bedrock/` routing
> convention below are doc-derived and must get **one live `curl` confirmation** against the
> deployed gateway before the catalog is considered final (see Open Questions).

## Architecture

A new **`bedrock` provider** in atom's model registry, additive to the existing four. Each
Bedrock model carries a **`wire`** discriminator (`"anthropic"` | `"openai"`) that selects both
the Bifrost drop-in endpoint suffix and the LangChain class used to construct it.

```
workflow YAML  model: bedrock-opus
      │
      ▼
TaskDef.model ─► engine.run_agent(override_model=…) ─► runtime.prepare_model
      │
      ▼
agent.prepare_model ─► resolve_spec("bedrock-opus") ─► build_model("bedrock-opus", thinking=…)
      │                                                        │
      │                                          provider == "bedrock"  (new branch)
      │                                                        │
      │                        ┌───────────────────────────────┴───────────────────────┐
      │                 wire == "anthropic"                                     wire == "openai"
      │                        │                                                        │
      │        ChatAnthropic(base_url=<root>/anthropic,                 ChatOpenAI(base_url=<root>/openai,
      │           model="bedrock/us.anthropic.claude-opus-4-8",            model="bedrock/qwen.qwen3-…",
      │           default_headers={"x-bf-vk": <key>}, …)                   default_headers={"x-bf-vk": <key>}, …)
      ▼
PreparedModel(model, caps, context_window)  ─► create_agent(model=…)
```

No changes are needed in `engine.py`, `runtime.py`, `agent.py`, `config/schema.py`, or `cli.py`.

## Component changes

### 1. Environment contract (gateway in env)

Two new environment variables, read at model-construction time and documented in `.env.example`:

| Variable | Meaning | Example |
|---|---|---|
| `ATOM_BIFROST_BASE_URL` | Bifrost gateway **root** (no integration suffix) | `https://bifrost.example.com` |
| `ATOM_BIFROST_API_KEY` | Bifrost virtual key, sent as `x-bf-vk` | `vk_live_…` |

`.env.example` gains a documented block for these. `.env` loading already happens in
`cli.py` (`load_dotenv()`), so no loader change is required. No AWS credentials are referenced by
atom.

### 2. Registry (`src/atom/models/registry.py`)

- `Provider` literal gains `"bedrock"`:
  `Literal["anthropic", "openai", "google_genai", "qwen", "bedrock"]`.
- `ModelSpec` gains one optional field:
  `wire: Literal["anthropic", "openai"] | None = None` (only meaningful when
  `provider == "bedrock"`; `None` for all existing specs).
- Eight new `REGISTRY` entries. Keys are prefixed **`bedrock-`** so they never collide with the
  direct `haiku`/`sonnet`/`opus` keys. Each has `init_str=None` (custom-factory path),
  `base_url=None` (the gateway root is env-sourced, not baked into the registry), and
  `model_name` = the bare **`bedrock-runtime`** model ID (the `bedrock/` prefix is added in
  `build_model`, not stored here). `api_key_env` is set to `ATOM_BIFROST_API_KEY` for
  documentation parity, though the bedrock branch reads the env var explicitly.

| key | wire | `model_name` (Bedrock runtime ID) | ctx | max out | vision | reasoning |
|---|---|---|---|---|---|---|
| `bedrock-opus` | anthropic | `us.anthropic.claude-opus-4-8` | 1_000_000 | 128_000 | ✅ | ✅ (adaptive-only) |
| `bedrock-sonnet` | anthropic | `us.anthropic.claude-sonnet-5` | 1_000_000 | 128_000 | ✅ | ✅ (always-on) |
| `bedrock-haiku` | anthropic | `anthropic.claude-haiku-4-5-20251001-v1:0` | 200_000 | 64_000 | ✅ | ✅ |
| `bedrock-qwen-coder` | openai | `qwen.qwen3-coder-480b-a35b-v1:0` | 131_072 | 16_384 | ❌ | ❌ |
| `bedrock-qwen` | openai | `qwen.qwen3-235b-a22b-2507-v1:0` | 262_144 | 8_192 | ❌ | ✅ |
| `bedrock-kimi-thinking` | openai | `moonshot.kimi-k2-thinking` | 262_144 | 16_384 | ❌ | ✅ |
| `bedrock-kimi` | openai | `moonshotai.kimi-k2.5` | 262_144 | 16_384 | ✅ | ❌ |
| `bedrock-gpt-oss` | openai | `openai.gpt-oss-120b-1:0` | 131_072 | 16_384 | ❌ | ✅ |

Notes on specific IDs (from the Bedrock model cards, to be curl-verified):
- Anthropic models need a **cross-region inference-profile prefix** on `bedrock-runtime`;
  `us.` is chosen as the default geo. The `bedrock/` gateway prefix is prepended to the whole
  string (e.g. `bedrock/us.anthropic.claude-opus-4-8`).
- Kimi K2 Thinking's runtime ID uses the `moonshot.` prefix (the mantle ID uses `moonshotai.`);
  Kimi K2.5 uses `moonshotai.` on both endpoints.
- `gpt-oss` uses the **versioned** runtime ID `openai.gpt-oss-120b-1:0` (not the mantle
  `openai.gpt-oss-120b`).

`resolve_spec` is **not** extended to synthesize raw `bedrock:<model>` strings — Bedrock models
require a `wire` + gateway routing that a bare `provider:model` string cannot express, so they are
reachable only via their registry keys. The provider whitelist for raw strings (line ~80) is left
unchanged.

### 3. Factory (`build_model`) — new `bedrock` branch

A third construction branch, mirroring the existing Qwen custom branch, inserted before/around
the current `init_str is None` fallthrough:

```python
if spec.provider == "bedrock":
    import os
    root = os.environ.get("ATOM_BIFROST_BASE_URL")
    api_key = os.environ.get("ATOM_BIFROST_API_KEY")
    if not root or not api_key:
        raise RuntimeError(
            "Bedrock models require ATOM_BIFROST_BASE_URL and ATOM_BIFROST_API_KEY to be set."
        )
    root = root.rstrip("/")
    common = dict(
        model=f"bedrock/{spec.model_name}",
        api_key=api_key,                       # carried anyway; x-bf-vk is authoritative
        default_headers={"x-bf-vk": api_key},
        **kwargs,                              # already includes max_retries/timeout + thinking
    )
    if spec.wire == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(base_url=f"{root}/anthropic", **common)
    if spec.wire == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(base_url=f"{root}/openai", **common)
    raise RuntimeError(f"Bedrock spec '{spec.key}' has an invalid wire: {spec.wire!r}")
```

The exact constructor keyword names (`base_url`, `default_headers`, `api_key`, and the
Anthropic `thinking` kwarg) will be confirmed against the current `langchain-anthropic` /
`langchain-openai` docs during implementation (see Plan). `ChatAnthropic` historically exposes
the base URL as `base_url` (aliased from `anthropic_api_url`); this is the one API-surface item to
verify before finalizing.

### 4. Reasoning / thinking (`_thinking_overrides`)

Add a `bedrock` branch dispatched on `spec.wire`:

- **`wire == "anthropic"`** — reuse the existing Anthropic translation so native `thinking`
  blocks flow through `/anthropic`. To avoid duplication, factor the current Anthropic body
  (lines ~140–149) into a small helper `_anthropic_thinking(spec, thinking, off)` and call it
  from both the `anthropic` branch and the `bedrock`/anthropic-wire branch.
  - **Fix required:** the adaptive-mode check currently reads
    `spec.model_name.startswith("claude-opus")`. Bedrock IDs are prefixed
    (`us.anthropic.claude-opus-4-8`), so change the test to a substring match
    (`"claude-opus" in spec.model_name`) so Opus-on-Bedrock still gets `type: "adaptive"`.
    This also keeps the existing direct-Anthropic behavior intact.
- **`wire == "openai"`** — Bifrost expects OpenAI-style reasoning via `extra_body`:
  ```python
  # reasoning-capable openai-wire bedrock models only:
  if off or not spec.supports_reasoning:
      return {}
  budget = thinking if isinstance(thinking, int) else _EFFORT_BUDGETS.get(thinking, _EFFORT_BUDGETS["medium"])
  return {"extra_body": {"reasoning": {"max_tokens": max(budget, 1024)}}}   # Bifrost min budget = 1024
  ```
  Non-reasoning models (`supports_reasoning == False`) emit nothing. This OpenAI-wire reasoning
  passthrough is **best-effort/unverified** through Bifrost→Bedrock and is flagged as such; it is
  the one behavior that most needs a live check.

Model-specific caveats to respect (documented, handled where cheap; otherwise verified live):
- Opus 4.7/4.8 are **adaptive-only** — an explicit `enabled` + `budget_tokens` block returns
  HTTP 400. The adaptive path above covers `thinking="adaptive"`; an explicit int budget on
  `bedrock-opus` would still emit `enabled` and should be avoided (documented; can add a guard).
- Sonnet 5 / Fable 5 thinking is **always-on**; sending a config is tolerated (effort
  configurable) but not required.
- Opus 4.7+ reject `temperature`/`top_p`/`top_k`. atom does not set a default temperature, so
  this is expected to be a non-issue; verified during implementation.

### 5. Error handling

- Requesting a `bedrock-*` model with either env var unset → `RuntimeError` naming both
  variables (raised in `build_model`, surfaced through the normal run/error path).
- An invalid/absent `wire` on a bedrock spec → `RuntimeError` (guards against a malformed
  registry entry).

### 6. Capability / profile fallbacks

`profiles.py` (`model_caps`, `resolve_context_window`) prefers a live `model.profile` (models.dev
data) and falls back to the static `ModelSpec` fields. Bedrock models routed through Bifrost will
generally **not** have a live profile, so the static `context_window` / `max_output_tokens` /
vision / reasoning fields in the registry are what get used — they are set deliberately in the
table above. No change to `profiles.py`.

## Testing (`tests/test_models.py`)

Mirror the existing Qwen monkeypatch pattern (patch the client classes; **no network**). New
tests assert:

1. **Dispatch by wire** — `bedrock-opus` builds a (faked) `ChatAnthropic` and
   `bedrock-qwen-coder` builds a (faked) `ChatOpenAI`; neither goes through `init_chat_model`.
2. **Base URL suffix** — anthropic-wire → `<root>/anthropic`, openai-wire → `<root>/openai`,
   with the trailing slash on the root normalized.
3. **Virtual-key header** — `default_headers` contains `{"x-bf-vk": <ATOM_BIFROST_API_KEY>}`.
4. **Model prefix** — the constructed `model` is `bedrock/<runtime id>` (e.g.
   `bedrock/us.anthropic.claude-opus-4-8`).
5. **Env sourcing** — base URL + key are read from `ATOM_BIFROST_BASE_URL` /
   `ATOM_BIFROST_API_KEY` (set via `monkeypatch.setenv`).
6. **Retry/timeout invariants** — `max_retries == 1`, `timeout == 120.0` still hold on the new
   branch; explicit overrides still win.
7. **Thinking translation** — anthropic-wire reuses the Anthropic block (incl. the Opus adaptive
   fix: `bedrock-opus` + `thinking="adaptive"` → `{"thinking": {"type": "adaptive"}}`);
   openai-wire reasoning-capable model + a budget → `{"extra_body": {"reasoning": {"max_tokens": ≥1024}}}`;
   non-reasoning openai-wire model → no thinking kwargs.
8. **Missing-env error** — building a `bedrock-*` model with the env vars unset raises
   `RuntimeError` naming both variables.

Existing `test_models.py` assertions (Qwen dispatch, thinking translations for the four current
providers, `clamp_concurrency`) must continue to pass unchanged.

## Explicitly out of scope (YAGNI)

- `langchain-aws` / `boto3` / native Bedrock SigV4 — not needed; Bifrost handles AWS server-side.
- Mantle-only models unreachable via Bifrost's Converse path (GPT-5.x family, Claude Mythos
  variants).
- Raw `bedrock:<model>` string resolution in `resolve_spec`.
- Per-model base-URL overrides / multiple gateways.
- YAML-declarable model catalog (models stay in `registry.py`).
- Live-streaming-of-reasoning verification through the `/openai` path (follow-up if the UI needs
  streamed reasoning tokens for openai-wire models).

## Open questions (verify before/at implementation)

1. **Live `curl` confirmation** of: the `/openai` + `/anthropic` suffix behavior on the deployed
   gateway, the `bedrock/<id>` routing string, and the exact reasoning param key
   (`reasoning.max_tokens` vs `reasoning.effort`). One `curl` per wire settles all three.
2. **Exact model IDs + cross-region prefixes** for the eight curated models against the current
   Bedrock model cards / the gateway's model catalog (esp. `bedrock-haiku`'s full runtime ID and
   whether Sonnet 5 / Haiku need a `us.` prefix in your region).
3. **AWS region** the Bifrost server runs in (governs in-region availability for the
   no-cross-region families — Qwen/Kimi/gpt-oss are in-region-only).
4. **Constructor API surface** — confirm `ChatAnthropic(base_url=…, default_headers=…, thinking=…)`
   and `ChatOpenAI(base_url=…, default_headers=…, extra_body=…)` against current
   `langchain-anthropic` / `langchain-openai` docs.

## Rollout / verification

1. Unit tests (`pytest tests/test_models.py`) — fully offline, gate the change.
2. Manual smoke test against the real gateway: set the two env vars, run a trivial agent with
   `model: bedrock-haiku` (cheapest anthropic-wire) and `model: bedrock-gpt-oss` (openai-wire),
   confirm a completion and a thinking round-trip.
3. Optionally point the `secassess` workflow (or a scratch workflow) at a `bedrock-*` model to
   confirm end-to-end parity with the existing providers.
