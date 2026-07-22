# AWS Bedrock via Bifrost — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a curated set of AWS Bedrock–hosted models (Claude, Qwen, Kimi, gpt-oss) to atom, routed through a self-hosted Bifrost gateway addressed by a custom base URL.

**Architecture:** A new additive `bedrock` provider in `src/atom/models/registry.py`. Each Bedrock model carries a `wire` discriminator (`anthropic`|`openai`) selecting a Bifrost drop-in endpoint (`<root>/anthropic` or `<root>/openai`) and a LangChain client class (`ChatAnthropic` or `ChatOpenAI`). Gateway root + virtual key come from env; models resolve through the existing `resolve_spec → build_model` path, so no other layer changes.

**Tech Stack:** Python, LangChain v1, `langchain-anthropic` (installed 1.4.8), `langchain-openai` (installed 1.3.3), pytest. Spec: `docs/superpowers/specs/2026-07-22-bedrock-via-bifrost-models-design.md`.

## Global Constraints

- **No new dependencies.** Use only `langchain-openai` and `langchain-anthropic` (already installed). Do NOT add `langchain-aws` / `boto3`.
- **Additive.** Do not modify or remove existing registry entries. All existing tests in `tests/test_models.py` must keep passing unchanged.
- **Retry/timeout invariant.** `max_retries` defaults to `1` and `timeout` to `DEFAULT_REQUEST_TIMEOUT_SECONDS` (120.0) on the new branch, exactly as the other providers; explicit overrides still win.
- **Gateway from env.** `ATOM_BIFROST_BASE_URL` (gateway root, no suffix) and `ATOM_BIFROST_API_KEY` (Bifrost virtual key). atom appends `/anthropic` or `/openai`. The key is sent in the `x-bf-vk` header (and as `api_key`).
- **Model routing string.** Send the model as `bedrock/<bedrock-runtime id>` (the `bedrock/` prefix is added in `build_model`, not stored in the registry).
- **Never** use `init_chat_model("bedrock/...")` — it dispatches to LangChain's native boto3/SigV4 path and bypasses the gateway. Always construct `ChatAnthropic`/`ChatOpenAI` explicitly with `base_url`.
- **Bifrost reasoning floor.** OpenAI-wire reasoning budget is floored to 1024 tokens.
- Verified constructor kwargs (installed versions): `ChatAnthropic(model=, base_url=, api_key=, default_headers=, thinking=, max_retries=, timeout=)`; `ChatOpenAI(model=, base_url=, api_key=, default_headers=, extra_body=, max_retries=, timeout=)`.

---

### Task 1: Registry schema + Bedrock model entries

**Files:**
- Modify: `src/atom/models/registry.py` (Provider literal line 18; `ModelSpec` dataclass lines 25-36; `REGISTRY` dict, append after line 67)
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: existing `ModelSpec`, `resolve_spec`.
- Produces: `ModelSpec.wire: Literal["anthropic","openai"] | None = None`; provider literal value `"bedrock"`; eight registry keys `bedrock-opus`, `bedrock-sonnet`, `bedrock-haiku`, `bedrock-qwen-coder`, `bedrock-qwen`, `bedrock-kimi-thinking`, `bedrock-kimi`, `bedrock-gpt-oss`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_models.py`:

```python
def test_bedrock_registry_entries_present_and_typed():
    opus = resolve_spec("bedrock-opus")
    assert opus.provider == "bedrock"
    assert opus.wire == "anthropic"
    assert opus.model_name == "us.anthropic.claude-opus-4-8"
    assert opus.init_str is None          # custom-factory path, not init_chat_model
    assert opus.context_window == 1_000_000
    assert opus.max_output_tokens == 128_000

    coder = resolve_spec("bedrock-qwen-coder")
    assert coder.wire == "openai"
    assert coder.supports_reasoning is False
    assert coder.model_name == "qwen.qwen3-coder-480b-a35b-v1:0"

    # All eight bedrock-* keys exist and are the bedrock provider with a wire set.
    keys = ["bedrock-opus", "bedrock-sonnet", "bedrock-haiku", "bedrock-qwen-coder",
            "bedrock-qwen", "bedrock-kimi-thinking", "bedrock-kimi", "bedrock-gpt-oss"]
    for k in keys:
        s = resolve_spec(k)
        assert s.provider == "bedrock"
        assert s.wire in ("anthropic", "openai")
        assert s.base_url is None          # gateway root is env-sourced, not baked in
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_models.py::test_bedrock_registry_entries_present_and_typed -v`
Expected: FAIL — `KeyError: "Unknown model 'bedrock-opus'"` (entries not yet added).

- [ ] **Step 3: Write minimal implementation**

In `src/atom/models/registry.py`, extend the Provider literal (line 18):

```python
Provider = Literal["anthropic", "openai", "google_genai", "qwen", "bedrock"]
```

Add the `wire` field to `ModelSpec` (after `base_url` on line 36):

```python
    base_url: str | None = None
    wire: Literal["anthropic", "openai"] | None = None  # bedrock-only: Bifrost endpoint + client class
```

Append the eight entries inside `REGISTRY`, after the Qwen block (after line 67, before the closing `}`):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_models.py::test_bedrock_registry_entries_present_and_typed -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/atom/models/registry.py tests/test_models.py
git commit -m "feat(models): add bedrock provider + wire field + curated Bedrock registry entries"
```

---

### Task 2: Thinking translation for the `bedrock` provider

**Files:**
- Modify: `src/atom/models/registry.py` (`_thinking_overrides`, lines 127-186 — extract Anthropic helper, add bedrock branch)
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: `ModelSpec`, `_EFFORT_BUDGETS`, `_coerce_thinking`.
- Produces: `_anthropic_thinking(spec: ModelSpec, thinking: Any, off: bool) -> dict[str, Any]`; `_thinking_overrides` now handles `spec.provider == "bedrock"` (anthropic-wire → native `thinking` dict; openai-wire reasoning-capable → `{"extra_body": {"reasoning": {"max_tokens": >=1024}}}`; else `{}`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_models.py`:

```python
def test_bedrock_anthropic_wire_reuses_anthropic_thinking():
    spec = resolve_spec("bedrock-opus")  # bedrock id: us.anthropic.claude-opus-4-8
    # adaptive must be recognized despite the "us.anthropic." prefix (substring match, not startswith)
    assert _thinking_overrides(spec, "adaptive") == {"thinking": {"type": "adaptive"}}
    assert _thinking_overrides(spec, 8192) == {"thinking": {"type": "enabled", "budget_tokens": 8192}}
    assert _thinking_overrides(spec, "off") == {}


def test_bedrock_openai_wire_reasoning_passthrough():
    spec = resolve_spec("bedrock-kimi-thinking")  # supports_reasoning=True, wire=openai
    assert _thinking_overrides(spec, "high") == {"extra_body": {"reasoning": {"max_tokens": 24576}}}
    assert _thinking_overrides(spec, 500) == {"extra_body": {"reasoning": {"max_tokens": 1024}}}  # floored
    assert _thinking_overrides(spec, "off") == {}


def test_bedrock_openai_wire_no_reasoning_for_nonreasoning_model():
    spec = resolve_spec("bedrock-qwen-coder")  # supports_reasoning=False
    assert _thinking_overrides(spec, "high") == {}


def test_direct_anthropic_thinking_unchanged_after_refactor():
    # Regression: the existing direct-Anthropic behavior must be identical after extracting the helper.
    assert _thinking_overrides(resolve_spec("opus"), "adaptive") == {"thinking": {"type": "adaptive"}}
    assert _thinking_overrides(resolve_spec("haiku"), 16000) == {
        "thinking": {"type": "enabled", "budget_tokens": 16000}}
    assert _thinking_overrides(resolve_spec("haiku"), "adaptive")["thinking"]["type"] == "enabled"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_models.py::test_bedrock_anthropic_wire_reuses_anthropic_thinking tests/test_models.py::test_bedrock_openai_wire_reasoning_passthrough tests/test_models.py::test_bedrock_openai_wire_no_reasoning_for_nonreasoning_model -v`
Expected: FAIL — bedrock provider hits the `return {}` fallthrough, so all three assert-non-empty cases fail (e.g. `{} != {"thinking": {"type": "adaptive"}}`).

- [ ] **Step 3: Write minimal implementation**

In `src/atom/models/registry.py`, add the helper immediately above `_thinking_overrides` (before line 127):

```python
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
```

Replace the existing Anthropic branch body (lines 140-149) with a call to the helper:

```python
    if spec.provider == "anthropic":
        return _anthropic_thinking(spec, thinking, off)
```

Add the bedrock branch immediately before the final `return {}` (line 186):

```python
    if spec.provider == "bedrock":
        if spec.wire == "anthropic":
            return _anthropic_thinking(spec, thinking, off)
        # openai-wire: Bifrost maps OpenAI-style reasoning -> Bedrock thinkingConfig (best-effort).
        if off or not spec.supports_reasoning:
            return {}
        budget = thinking if isinstance(thinking, int) else _EFFORT_BUDGETS.get(thinking, _EFFORT_BUDGETS["medium"])
        return {"extra_body": {"reasoning": {"max_tokens": max(budget, 1024)}}}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_models.py -v`
Expected: PASS — all new bedrock thinking tests AND all pre-existing tests (esp. `test_adaptive_gated_to_opus`, `test_int_thinking_budget_coerced_and_applied`) still green.

- [ ] **Step 5: Commit**

```bash
git add src/atom/models/registry.py tests/test_models.py
git commit -m "feat(models): thinking translation for bedrock wires (anthropic reuse + openai extra_body)"
```

---

### Task 3: `build_model` Bedrock construction branch

**Files:**
- Modify: `src/atom/models/registry.py` (`build_model`, lines 189-208 — add bedrock dispatch; add `_build_bedrock` helper)
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: `resolve_spec`, `_thinking_overrides`, `DEFAULT_REQUEST_TIMEOUT_SECONDS`, `ModelSpec.wire`, env vars `ATOM_BIFROST_BASE_URL` / `ATOM_BIFROST_API_KEY`.
- Produces: `_build_bedrock(spec: ModelSpec, kwargs: dict[str, Any]) -> BaseChatModel`; `build_model` returns a `ChatAnthropic` (anthropic-wire) or `ChatOpenAI` (openai-wire) with `base_url=<root>/<wire>`, `model="bedrock/<id>"`, `default_headers={"x-bf-vk": key}`; raises `RuntimeError` when env is unset.

- [ ] **Step 1: Write the failing test**

Add `import pytest` to the top of `tests/test_models.py` (after the existing `from __future__` line), then append:

```python
def test_build_model_bedrock_anthropic_wire(monkeypatch):
    captured: dict = {}

    class FakeChatAnthropic:
        def __init__(self, **kw):
            captured.update(kw)
            captured["_cls"] = "anthropic"

    import langchain_anthropic
    monkeypatch.setattr(langchain_anthropic, "ChatAnthropic", FakeChatAnthropic)
    monkeypatch.setenv("ATOM_BIFROST_BASE_URL", "https://bifrost.example.com/")  # trailing slash
    monkeypatch.setenv("ATOM_BIFROST_API_KEY", "vk_test")

    build_model("bedrock-opus", thinking="adaptive")
    assert captured["_cls"] == "anthropic"
    assert captured["base_url"] == "https://bifrost.example.com/anthropic"  # slash normalized, suffix added
    assert captured["model"] == "bedrock/us.anthropic.claude-opus-4-8"
    assert captured["default_headers"] == {"x-bf-vk": "vk_test"}
    assert captured["api_key"] == "vk_test"
    assert captured["thinking"] == {"type": "adaptive"}
    assert captured["max_retries"] == 1
    assert captured["timeout"] == 120.0


def test_build_model_bedrock_openai_wire(monkeypatch):
    captured: dict = {}

    class FakeChatOpenAI:
        def __init__(self, **kw):
            captured.update(kw)
            captured["_cls"] = "openai"

    import langchain_openai
    monkeypatch.setattr(langchain_openai, "ChatOpenAI", FakeChatOpenAI)
    monkeypatch.setenv("ATOM_BIFROST_BASE_URL", "https://bifrost.example.com")
    monkeypatch.setenv("ATOM_BIFROST_API_KEY", "vk_test")

    build_model("bedrock-kimi-thinking", thinking="high")
    assert captured["_cls"] == "openai"
    assert captured["base_url"] == "https://bifrost.example.com/openai"
    assert captured["model"] == "bedrock/moonshot.kimi-k2-thinking"
    assert captured["default_headers"] == {"x-bf-vk": "vk_test"}
    assert captured["extra_body"] == {"reasoning": {"max_tokens": 24576}}


def test_build_model_bedrock_missing_env_raises(monkeypatch):
    monkeypatch.delenv("ATOM_BIFROST_BASE_URL", raising=False)
    monkeypatch.delenv("ATOM_BIFROST_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ATOM_BIFROST_BASE_URL"):
        build_model("bedrock-haiku")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_models.py::test_build_model_bedrock_anthropic_wire tests/test_models.py::test_build_model_bedrock_openai_wire tests/test_models.py::test_build_model_bedrock_missing_env_raises -v`
Expected: FAIL — `build_model` falls through to the Qwen branch for the bedrock specs and calls `ChatQwen`, so `captured` is empty (KeyError on `captured["_cls"]`); the missing-env test does not raise.

- [ ] **Step 3: Write minimal implementation**

In `src/atom/models/registry.py`, add the bedrock dispatch inside `build_model`, immediately after the `init_str is not None` block (after line 203) and before the Qwen comment:

```python
    if spec.provider == "bedrock":
        return _build_bedrock(spec, kwargs)
```

Add the helper immediately after `build_model` (after line 208):

```python
def _build_bedrock(spec: ModelSpec, kwargs: dict[str, Any]) -> BaseChatModel:
    """Construct a Bedrock model routed through the Bifrost gateway.

    Base URL (gateway root) and the Bifrost virtual key come from env. The key is carried in the
    ``x-bf-vk`` header (authoritative for Bifrost) and also as ``api_key``. The model is sent as
    ``bedrock/<runtime id>``; ``wire`` picks the drop-in endpoint suffix and the LangChain class.
    We construct the client classes directly (never ``init_chat_model``), which would otherwise
    dispatch to LangChain's native boto3/SigV4 Bedrock path and bypass the gateway.
    """
    import os

    root = os.environ.get("ATOM_BIFROST_BASE_URL")
    api_key = os.environ.get("ATOM_BIFROST_API_KEY")
    if not root or not api_key:
        raise RuntimeError(
            f"Model '{spec.key}' requires ATOM_BIFROST_BASE_URL and ATOM_BIFROST_API_KEY to be set."
        )
    root = root.rstrip("/")
    common: dict[str, Any] = dict(
        model=f"bedrock/{spec.model_name}",
        api_key=api_key,
        default_headers={"x-bf-vk": api_key},
        **kwargs,  # includes thinking/extra_body + max_retries/timeout
    )
    if spec.wire == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(base_url=f"{root}/anthropic", **common)
    if spec.wire == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(base_url=f"{root}/openai", **common)
    raise RuntimeError(f"Bedrock model '{spec.key}' has an invalid wire: {spec.wire!r}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_models.py -v`
Expected: PASS — all bedrock build tests plus every pre-existing test (Qwen dispatch, retry/timeout, overrides) still green.

- [ ] **Step 5: Commit**

```bash
git add src/atom/models/registry.py tests/test_models.py
git commit -m "feat(models): build_model bedrock branch (ChatAnthropic/ChatOpenAI via Bifrost, x-bf-vk auth)"
```

---

### Task 4: Document the gateway env vars

**Files:**
- Modify: `.env.example`

**Interfaces:**
- Consumes: nothing. Produces: documented `ATOM_BIFROST_BASE_URL` / `ATOM_BIFROST_API_KEY`.

- [ ] **Step 1: Add the env block**

Append to `.env.example` (after the DASHSCOPE line / before the `ATOM_HOME` block):

```bash

# AWS Bedrock via a Bifrost gateway (the bedrock-* models). Set the gateway ROOT URL only;
# atom appends /openai and /anthropic per model family. The virtual key is sent as the
# x-bf-vk header. No AWS credentials are needed client-side — Bifrost holds them server-side.
# ATOM_BIFROST_BASE_URL=https://bifrost.example.com
# ATOM_BIFROST_API_KEY=
```

- [ ] **Step 2: Verify the file reads cleanly**

Run: `grep -n "ATOM_BIFROST" .env.example`
Expected: two matching commented lines printed.

- [ ] **Step 3: Commit**

```bash
git add .env.example
git commit -m "docs(env): document ATOM_BIFROST_BASE_URL / ATOM_BIFROST_API_KEY for Bedrock via Bifrost"
```

---

### Task 5: Live gateway verification (manual — requires the real Bifrost gateway + key)

**Files:** none (manual verification; do NOT commit secrets).

**Interfaces:** Consumes the deployed gateway. Produces confirmation (or correction) of the exact IDs, endpoint suffixes, and reasoning param key documented in the spec's Open Questions.

> This task cannot run in CI/offline. Run it once against the real gateway before relying on the models. If any check fails, fix the affected registry entry / reasoning mapping and re-run Tasks 1-3's tests.

- [ ] **Step 1: Smoke-test each wire with `curl`**

```bash
export ATOM_BIFROST_BASE_URL=https://<your-bifrost-host>
export ATOM_BIFROST_API_KEY=vk_<your-key>

# anthropic-wire (Claude on Bedrock)
curl -sS "$ATOM_BIFROST_BASE_URL/anthropic/v1/messages" \
  -H "x-bf-vk: $ATOM_BIFROST_API_KEY" -H "content-type: application/json" \
  -d '{"model":"bedrock/anthropic.claude-haiku-4-5-20251001-v1:0","max_tokens":64,"messages":[{"role":"user","content":"say hi"}]}'

# openai-wire (gpt-oss on Bedrock)
curl -sS "$ATOM_BIFROST_BASE_URL/openai/v1/chat/completions" \
  -H "x-bf-vk: $ATOM_BIFROST_API_KEY" -H "content-type: application/json" \
  -d '{"model":"bedrock/openai.gpt-oss-120b-1:0","messages":[{"role":"user","content":"say hi"}]}'
```

Expected: a 200 with a completion from each. Confirm: (a) the `/anthropic` and `/openai` suffixes are correct for your build, (b) the `bedrock/<id>` strings resolve (fix any ID / `us.` prefix that 404s), (c) note which reasoning param your gateway accepts (`reasoning.max_tokens` vs `reasoning.effort`) and adjust the Task 2 openai-wire mapping if needed.

- [ ] **Step 2: End-to-end via atom**

Run a trivial agent/workflow with `model: bedrock-haiku` and again with `model: bedrock-gpt-oss` (env vars exported). Confirm a completion and, for `bedrock-opus`, a thinking round-trip.

- [ ] **Step 3: Reconcile**

If IDs/prefixes/params differed, update `src/atom/models/registry.py` and the Task 2 mapping, re-run `.venv/bin/pytest tests/test_models.py -v`, and commit:

```bash
git add src/atom/models/registry.py
git commit -m "fix(models): reconcile Bedrock ids/reasoning params with live Bifrost gateway"
```

---

## Self-Review

**1. Spec coverage:**
- Env contract (spec §Component 1) → Task 4 + read in Task 3. ✅
- Registry: Provider literal, `wire` field, 8 entries (spec §Component 2) → Task 1. ✅
- `build_model` bedrock branch (spec §Component 3) → Task 3. ✅
- Thinking translation incl. Opus substring fix + openai-wire extra_body (spec §Component 4) → Task 2. ✅
- Error handling: missing env + invalid wire (spec §Component 5) → Task 3 (`RuntimeError`s + `test_build_model_bedrock_missing_env_raises`). ✅
- Capability fallbacks (spec §Component 6) → static fields set in Task 1; no code change needed. ✅
- Testing (spec §Testing, 8 assertions) → distributed across Tasks 1-3. ✅
- Out-of-scope items (no langchain-aws, no raw bedrock: strings, mantle-only excluded) → honored; `resolve_spec` untouched. ✅
- Open questions / live verification (spec §Open Questions) → Task 5. ✅

**2. Placeholder scan:** No TBD/TODO; every code step shows complete code; test bodies are concrete. ✅

**3. Type consistency:** `_anthropic_thinking(spec, thinking, off)` defined in Task 2 and called in both the anthropic and bedrock branches with the same signature. `_build_bedrock(spec, kwargs)` defined and called consistently. `wire` values `"anthropic"`/`"openai"` used identically in registry entries (Task 1), thinking branch (Task 2), and build branch (Task 3). Env var names `ATOM_BIFROST_BASE_URL` / `ATOM_BIFROST_API_KEY` identical across Tasks 3, 4, 5 and the tests. ✅
