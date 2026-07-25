# Cognis: Provider and Model Handling Stabilization

## Purpose

This spec captures stabilization work on the LLM provider/model handling layer that sits between the agent loop and LiteLLM. The review that produced
[`23-harness-stabilization.md`](23-harness-stabilization.md) found a family of
correctness issues in how Cognis translates reasoning effort, sampling
parameters, token budgets, prompt caching hints, and tool exposure flags into
real provider API calls.

The symptoms were observable in production as:

- reasoning models that behave "dumbly" on simple turns
- classifier and evaluator paths that take seconds for one-line JSON
- proxy-routed providers receiving capability flags meant for native providers
- non-Anthropic deployments running Claude models without prompt caching
- silent mis-resolution of providers when multiple providers share a model

This spec defines the correctness fixes at the provider layer, independent
from the broader harness stabilization work. It is the fourth and final
stabilization stage before the deferred structural work (typed deliverables,
step profiles, and workflow-first composition) lands in
[`31-workflow-deliverables implementation`](implementation/stage-31-workflow-deliverables.md)
and the later composition stage.

## Related Specs

- [`03-session-model.md`](03-session-model.md)
- [`05-integrations.md`](05-integrations.md)
- [`06-tool-system.md`](06-tool-system.md)
- [`13-nfr-operations.md`](13-nfr-operations.md)
- [`14-workflow-engine.md`](14-workflow-engine.md)
- [`23-harness-stabilization.md`](23-harness-stabilization.md)

## Why This Stage Exists

The provider layer is the translation boundary between Cognis internal types
and provider-specific API shapes (OpenAI, Anthropic, Gemini, Groq, proxies).
Bugs in this boundary are invisible at the Cognis level — the provider
silently accepts unknown parameters, returns a lower-quality answer, or
takes a round trip longer than needed. None of these failures show up as
clean exceptions, which is why the review prioritised them separately.

Three clusters drive the majority of the observed symptoms:

1. **Reasoning-effort translation** sends Cognis-internal sentinels such as
   `"default"` to real provider APIs, fails to strip parameters that
   reasoning models reject (`temperature`, `top_p`), and misses per-family
   translation rules for Anthropic extended thinking and Gemini
   `thinking_config`.
2. **Capability gating** applies Anthropic prompt caching and beta headers
   based on model-name regex rather than on `model_info` capability flags,
   so proxy-routed deployments either miss caching entirely or receive beta
   headers they cannot honour.
3. **Provider resolution and health** depend on non-deterministic row order,
   silently fall through to LiteLLM environment-variable lookup on
   mis-configured deployments, and cache proxy model info per URL without
   accounting for per-user API keys.

## Design Principles

### 1. Provider-internal translation happens in one place

All reasoning-effort, sampling-parameter, and capability-flag translation
lives in the provider layer (`cognis/providers/llm/`). Callers pass
Cognis-internal values and trust the provider to convert them. Agent loop
and auxiliary call sites never translate per-family themselves.

### 2. Capability flags are authoritative

`ModelInfo` capability flags (`supports_reasoning`,
`supports_prompt_caching`, `supports_defer_loading`,
`supports_openai_namespace_tools`) are the source of truth. Model-name
regex may be a fallback signal but never overrides an explicit flag.

### 3. Reasoning models are a distinct shape

Reasoning models reject sampling knobs (`temperature`, `top_p`, `top_k`)
and use `max_completion_tokens` instead of `max_tokens`. The provider
layer strips or translates these uniformly so that classifier, evaluator,
compaction, and follow-up call sites can ignore the distinction.

### 4. Fail loud on configuration gaps

Silent fall-through to LiteLLM env-var credentials hides misconfiguration
until a real turn fails. The provider layer refuses turns without a
resolved provider and reports clearly.

### 5. Deterministic provider resolution

When multiple providers share a model, resolution picks a deterministic
candidate: `is_default=True` first, then lexicographic `provider_id`. No
implicit dependency on DB insert order.

## Scope

### In Scope

- reasoning-effort translation correctness across OpenAI, Anthropic
  (including extended thinking and adaptive), Gemini, Groq, and generic
  reasoning models
- sampling-parameter stripping for reasoning models at provider layer
- `max_tokens` → `max_completion_tokens` translation for OpenAI reasoning
  models
- Anthropic `thinking.budget_tokens`/`max_tokens` invariant enforcement
- capability-flag gating for Anthropic prompt caching and beta tool-search
  header
- new capability flag `supports_openai_namespace_tools` with default-off
  behaviour
- deterministic multi-provider model resolution
- reverse-index caching for model → provider lookup
- proxy `/model/info` cache keyed by URL and hashed API key
- Responses bridge: reject or drop `role=tool` without `tool_call_id`;
  validated `response_format` translation; correct usage-field precedence
- agent-bootstrap defaults cleanup (`max_tokens=500`, fixed `temperature`
  values removed in favour of inheritance)
- agent `temperature` (and `top_p`) surfaced into the agent loop, guarded
  by the provider-layer strip
- telemetry counters for reasoning-effort use and sampling-parameter
  stripping
- workflow-step `reasoning_effort` override validation

### Out of Scope

- cost tracking, `Cost` calculation, and per-turn usage persistence
- additional sampling knobs (`top_k`, `frequency_penalty`,
  `presence_penalty`) beyond `top_p`
- deeper runtime-abstraction changes from specs 17-19 (deferred Stage 28)
- UI redesign beyond surfacing the session `/thinking` override in
  `/info` and session state
- typed deliverables and step profiles (deferred to Stage 31)
- workflow-first composition (deferred to the post-Stage-30 composition stage)

## Tracks and Required Changes

### P1. Reasoning Effort Correctness

1. Never emit literal `reasoning_effort="default"`. Omitted effort and
   `"default"` both mean the concrete model/provider default. For documented
   adaptive Claude models Cognis sends `thinking={"type":"adaptive"}` without
   `output_config.effort`, leaving Anthropic's effort default in control. Other
   providers continue receiving no reasoning control unless their contract
   requires one.
2. Implement `"none"` semantics per family:
   - Adaptive Anthropic models: send `thinking={"type":"disabled"}` only on
     models that support disabling. Claude Fable 5, Claude Mythos 5, and Claude
     Mythos Preview reject `"none"` because thinking is always on.
   - Older Anthropic models retain manual `budget_tokens` behavior and omit
     thinking when disabled.
   - Gemini: `thinking_config={"thinking_budget": 0}`.
   - OpenAI and other providers use only model-supported native values; default
     never implies or forces `"none"`.
3. Strip `temperature`, `top_p`, and `top_k` in the provider layer when
   the target model has `supports_reasoning=True`. Call sites no longer
   need to know.
4. Set explicit `reasoning_effort="minimal"` (OpenAI/Groq) or `"low"`
   (Anthropic, Gemini) for all auxiliary paths: classifier, evaluator,
   compaction, follow-up classifier.
5. Treat `model_info.supports_reasoning` as authoritative. Name regex
   remains a fallback only when the flag is unset.
6. Enforce `thinking.budget_tokens < max_tokens` for Anthropic extended
   thinking. Either lift `max_tokens` to a documented floor per level or
   cap `budget_tokens = max(1024, max_tokens - buffer)`.
7. Translate `max_tokens` → `max_completion_tokens` for OpenAI reasoning
   models explicitly in the provider layer rather than relying on
   LiteLLM heuristics.
8. Populate `supports_extended_thinking` from LiteLLM static info or
   remove the field. Wire the remaining budget-floor logic to it.
9. Validate workflow-step `reasoning_effort` overrides against
   `NORMALIZED_REASONING_LEVELS` and reject unknown values clearly.
10. Adaptive effort availability is model-specific. `xhigh` is exposed only
    for Claude Fable 5, Claude Mythos 5, Claude Opus 4.8/4.7, and Claude
    Sonnet 5; `max` is available on every documented adaptive model.

### P2. Health, Test, and Agent Bootstrap

1. `test_provider` uses `max_tokens >= 256` (1024 for reasoning models)
   and sets `reasoning_effort="minimal"` when the model supports
   reasoning so the probe completes with real content.
2. `health()` returns `status="unhealthy"` when zero providers are
   configured, `"degraded"` only for partial failure.
3. Remove `max_tokens=500` and fixed `temperature` defaults from the
   agent bootstrap route. New agents inherit from model routing and
   provider configuration.

### P3. Provider Config and Routing

1. Cache reverse index (`model_id → provider_id`) alongside the
   existing model-info cache with the same TTL.
2. Multi-provider resolution picks `is_default=True` first, then
   lexicographic `provider_id`. `find_provider_for_model` and
   `_find_provider_for_model` share the same rule.
3. Refuse turns when provider resolution returns `None` (except for
   explicit probe paths). Do not silently fall through to LiteLLM
   env-var lookup.
4. Strip `max_retries` from provider-supplied kwargs before passing to
   LiteLLM when the caller wraps with `with_llm_retry`. Prevents
   worst-case `3 × 3` retry amplification.
5. `_fetch_proxy_model_info` cache key includes a hash of the API key so
   shared proxy URLs cannot leak user-visible model lists across auth
   contexts.

### P4. Prompt Cache and Tool Exposure Gating

1. Gate Anthropic `cache_control` on `model_info.supports_prompt_caching`
   rather than the model-name regex.
2. Gate the Anthropic `anthropic-beta: tool-search-tool-2025-10-19`
   header on `model_info.supports_defer_loading` only.
3. For Anthropic defer-loading paths, set Anthropic's parallel-tool-use
   request field (`disable_parallel_tool_use: false`) explicitly where
   supported.
4. Add `supports_openai_namespace_tools` to `ModelInfo`, default
   `False`. Gate the OpenAI Responses `{"type":"namespace"}` and bare
   `{"type":"tool_search"}` emissions behind this flag. LiteLLM-proxy
   providers that actually accept these custom shapes can be set to
   `True` through DB config or proxy discovery.
5. When `supports_openai_namespace_tools=False` and the Responses API is
   in use, fall back to a flat tool list with `defer_loading: true` per
   the public OpenAI Responses schema.

### P5. Responses Bridge Robustness

1. Reject or drop `role=tool` messages that lack `tool_call_id`. Do not
   generate synthetic call ids that will fail provider-side validation.
2. Validate `response_format` shape before forwarding as `text.format`.
   Accept structured dicts or one of `"json"`, `"json_object"`,
   `"text"`; log and drop other strings.
3. Prefer Responses-native `input_tokens` / `output_tokens` when
   extracting usage from Responses payloads. Preserve
   `prompt_tokens`/`completion_tokens` only for chat-completions
   responses.

### P6. Agent Config, Sampling, and Telemetry

1. Thread `agent.llm_config.temperature` into the main agent loop's
   request kwargs. The provider-layer strip (P1) continues to guard
   reasoning models.
2. Add `top_p` to `AgentLLMConfig` and surface it through the same
   path.
3. Add Prometheus counter
   `cognis_llm_reasoning_effort_used_total{family, level}` at the point
   we send effort to the provider.
4. Add Prometheus counter
   `cognis_llm_sampling_params_stripped_total{reason}` when
   `temperature`/`top_p`/`top_k` are removed.
5. Add Prometheus counter `cognis_llm_max_tokens_translated_total` when
   `max_tokens` is translated to `max_completion_tokens`.
6. Surface the session `/thinking` override in `/info` and in the
   WebSocket session state so UI can render it.

## Acceptance Criteria

- No provider call carries a literal `reasoning_effort="default"`.
- Every reasoning-model call omits `temperature` and `top_p`.
- Classifier, evaluator, compaction, and follow-up call sites run with
  an explicit low-effort reasoning hint and complete within expected
  latency on reasoning providers.
- Anthropic cache control is active for every
  `supports_prompt_caching=True` model regardless of model name.
- The Anthropic tool-search beta header is sent only to
  `supports_defer_loading=True` models.
- OpenAI Responses namespace tool shape is sent only when
  `supports_openai_namespace_tools=True`.
- Turns fail loudly and quickly when no provider resolves.
- Multi-provider resolution is deterministic across runs.
- Proxy model-info caching does not leak across API keys.
- Workflow-step `reasoning_effort` overrides reject unknown values.
- Agent bootstrap creates new agents without `temperature` or
  `max_tokens` defaults that override model routing.

## Testing Requirements

### Unit Tests

- `apply_reasoning_config` matrix across every (family × normalised
  level), including `"default"` → key absent for non-adaptive families.
- Provider-layer strip of sampling params for reasoning models.
- Auxiliary call sites (classifier, evaluator, compaction, follow-ups)
  send explicit `reasoning_effort` and no `temperature`.
- Anthropic thinking invariant: `budget_tokens < max_tokens`.
- OpenAI `max_tokens → max_completion_tokens` translation.
- Capability-gated prompt caching and tool-search beta header.
- OpenAI Responses namespace-tool emission gated by
  `supports_openai_namespace_tools`.
- Multi-provider resolution determinism.
- Proxy model-info cache isolated by hashed API key.
- Responses bridge rejects missing `tool_call_id`.
- Workflow-step reasoning-effort validation rejects unknown values.

### Integration Tests

- End-to-end chat turn through `system:direct` (with
  `reasoning_effort="default"`) against mock Anthropic adaptive,
  Anthropic non-adaptive, OpenAI reasoning, and Gemini reasoning
  models — request kwargs match expected translation.
- Evaluator path on a reasoning model completes in expected latency
  with `reasoning_effort="minimal"` and yields a valid JSON decision.
- Provider test route succeeds for a reasoning-capable model.

### Contract Tests

- Against a real LiteLLM proxy declaring
  `supports_openai_namespace_tools=True`: namespace tool shape is
  accepted and deferred tools resolve.
- Against a real OpenAI deployment with the flag unset: flat tool list
  with `defer_loading: true` is accepted.

## Telemetry

Add counters and log fields to validate the new behaviour:

- `cognis_llm_reasoning_effort_used_total{family, level}`
- `cognis_llm_sampling_params_stripped_total{reason}`
- `cognis_llm_max_tokens_translated_total`
- `cognis_llm_cache_control_applied_total{gated_by}`
  (`"capability_flag"` vs `"name_regex"`) — temporary, retired once P4
  lands everywhere.

Content, prompts, tool arguments, and memory bodies remain excluded
from logs.

## Migration and Backwards Compatibility

- No schema migrations are required. `supports_openai_namespace_tools`
  is a new optional `ModelInfo` field with default `False`.
- Existing LiteLLM-proxy deployments relying on the custom namespace
  tool shape must set the flag on their proxy provider configuration
  (or receive it via proxy discovery).
- Agent records with `temperature` or `max_tokens` set continue to
  work; the provider-layer strip guards reasoning models from rejecting
  those parameters.

## Deferred

- Full cost and usage tracking (reasoning-token and cache-token
  breakdowns, `Cost` population, per-turn persistence).
- Additional sampling knobs beyond `temperature` and `top_p`.
- Typed deliverables and step profiles (see
  [`21-workflow-deliverables.md`](21-workflow-deliverables.md),
  [`22-step-profiles.md`](22-step-profiles.md) — implemented in
  [`stage-31-workflow-deliverables.md`](implementation/stage-31-workflow-deliverables.md)).
