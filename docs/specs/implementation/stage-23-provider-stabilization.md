# Stage 23: Provider and Model Handling Stabilization

## Status

DONE

## Goal

Correct the LLM provider/model handling layer so reasoning effort,
sampling parameters, token budgets, capability flags, and provider
resolution all produce valid, deterministic provider API calls.

This stage implements [`../24-provider-stabilization.md`](../24-provider-stabilization.md)
across backend code, tests, and telemetry. It completes the
four-stage harness stabilization sequence before the deferred typed
deliverables and step-profile work in Stage 24.

## Dependencies

- `docs/specs/03-session-model.md`
- `docs/specs/05-integrations.md`
- `docs/specs/06-tool-system.md`
- `docs/specs/13-nfr-operations.md`
- `docs/specs/14-workflow-engine.md`
- `docs/specs/23-harness-stabilization.md`
- `docs/specs/24-provider-stabilization.md`
- Stages 20 and 21 complete; Stage 22 in progress or complete.

## Scope

### In Scope

- reasoning-effort translation correctness across OpenAI, Anthropic
  (including extended thinking and adaptive), Gemini, Groq, and generic
  reasoning models
- sampling-parameter stripping for reasoning models at provider layer
- `max_tokens` → `max_completion_tokens` translation for OpenAI
  reasoning models
- Anthropic `thinking.budget_tokens`/`max_tokens` invariant enforcement
- capability-flag gating for Anthropic prompt caching and the
  tool-search beta header
- new capability flag `supports_openai_namespace_tools` with default-off
  behaviour and namespace-tool gating
- deterministic multi-provider model resolution and reverse-index cache
- proxy `/model/info` cache keyed by URL and hashed API key
- Responses bridge robustness (missing tool_call_id, response_format
  validation, usage-field precedence)
- agent-bootstrap defaults cleanup and agent `temperature`/`top_p`
  wiring guarded by the provider-layer strip
- telemetry counters for reasoning-effort use, sampling-parameter
  stripping, `max_tokens` translation, and cache-control gating
- workflow-step `reasoning_effort` override validation
- unit, integration, and contract test coverage for the above

### Out of Scope

- cost tracking, `Cost` calculation, per-turn usage persistence
- sampling knobs beyond `temperature` and `top_p`
- runtime-abstraction changes from specs 17-19
- typed deliverables and step profiles (Stage 24)

## Deliverables

### 1. Reasoning effort translation

- update `cognis/providers/llm/reasoning.py` so that:
  - `"default"` removes the `reasoning_effort` key for every family
    except `anthropic_adaptive` (which continues to emit
    `thinking={"type":"adaptive"}`);
  - `"none"` is translated per family (Anthropic: omit `thinking` or
    `thinking={"type":"disabled"}`; Gemini:
    `thinking_config={"thinking_budget": 0}`; OpenAI/Groq: `"minimal"`);
  - `_supports_reasoning` uses `model_info.supports_reasoning` as
    authoritative, falling back to the name regex only when unset;
- add a new provider-layer helper that strips `temperature`, `top_p`,
  and `top_k` when the target model is a reasoning model, called from
  `generate` and `stream_generate` right after `apply_reasoning_config`;
- translate `max_tokens` → `max_completion_tokens` for OpenAI reasoning
  models in the same path;
- populate `supports_extended_thinking` from LiteLLM static info or
  remove the field, and wire Anthropic `thinking.budget_tokens`
  enforcement to it;
- validate workflow-step `reasoning_effort` overrides against
  `NORMALIZED_REASONING_LEVELS` in
  `cognis/core/workflow_registry.py`.

### 2. Auxiliary call-site effort hints

- set `reasoning_effort="minimal"` (OpenAI/Groq) or `"low"` (Anthropic,
  Gemini) explicitly in:
  - `cognis/core/decision.py` classifier call
  - `cognis/core/followups.py` follow-up classifier
  - `cognis/core/step_evaluator.py` evaluator call
  - `cognis/core/compaction.py` compaction call
- call sites no longer pass `temperature=0` for reasoning-capable
  models; the provider-layer strip handles it. Non-reasoning models
  continue to receive `temperature=0` as today.

### 3. Health, test, and agent bootstrap

- `LiteLLMProvider.test_provider` uses `max_tokens >= 256` (1024 for
  reasoning models) and sets `reasoning_effort="minimal"` when the
  target supports reasoning.
- `LiteLLMProvider.health` returns `status="unhealthy"` when zero
  providers are configured.
- `cognis/api/routes/agents.py` removes default `max_tokens=500` and
  fixed `temperature` values on agent bootstrap. New agents inherit
  from model routing and provider config.

### 4. Provider resolution and caching

- reverse index (`model_id → provider_id`) cached alongside the
  model-info cache, same TTL;
- unified resolution rule used by both
  `_find_provider_for_model` and `find_provider_for_model`:
  `is_default=True` first, then lexicographic `provider_id`;
- turn fails loudly when provider resolution returns `None` outside
  explicit probe paths;
- `_resolve_provider_kwargs`/`_provider_request_kwargs` strip
  `max_retries` so it does not combine with `with_llm_retry`;
- `_fetch_proxy_model_info` cache key includes a hash of the API key.

### 5. Prompt cache and tool exposure gating

- `cognis/providers/llm/litellm.py` gates `cache_control` hint on
  `model_info.supports_prompt_caching`;
- `cognis/core/tool_exposure.py` gates the Anthropic
  `tool-search-tool-2025-10-19` beta header on
  `model_info.supports_defer_loading`;
- Anthropic defer-loading paths explicitly set
  `disable_parallel_tool_use=false` when supported;
- `ModelInfo` gains `supports_openai_namespace_tools: bool = False`
  (populated from LiteLLM static info and DB overrides) and the OpenAI
  Responses namespace tool shape is emitted only when the flag is true;
  otherwise the tool-exposure layer falls back to a flat tool list
  with `defer_loading: true`.

### 6. Responses bridge robustness

- `messages_to_responses_input` drops `role=tool` messages that lack
  `tool_call_id` (with a warning) instead of synthesising ids;
- `responses_request_kwargs` validates `response_format` shape and
  only forwards structured dicts or one of `"json"`, `"json_object"`,
  `"text"`;
- `_extract_usage` prefers `input_tokens`/`output_tokens` for
  Responses payloads.

### 7. Agent config and sampling

- thread `agent.llm_config.temperature` into the main agent loop
  kwargs;
- add `top_p: float | None` to `AgentLLMConfig` and surface it through
  the same path;
- both remain guarded by the reasoning-model strip from Deliverable 1.

### 8. Telemetry and validation

- add Prometheus counters:
  - `cognis_llm_reasoning_effort_used_total{family, level}`
  - `cognis_llm_sampling_params_stripped_total{reason}`
  - `cognis_llm_max_tokens_translated_total`
  - `cognis_llm_cache_control_applied_total{gated_by}`
- log structured fields (no content) for reasoning-effort decisions
  and stripped parameters.

### 9. Tests

- unit tests for:
  - reasoning-effort translation matrix per family × normalised level
  - sampling-parameter stripping for reasoning models
  - `max_tokens` → `max_completion_tokens` translation
  - Anthropic `thinking.budget_tokens`/`max_tokens` invariant
  - capability-gated Anthropic cache control and tool-search header
  - namespace tool emission gated by
    `supports_openai_namespace_tools`
  - multi-provider resolution determinism
  - proxy model-info cache isolation by API-key hash
  - Responses bridge handling of missing `tool_call_id`
  - workflow-step `reasoning_effort` validation
- integration tests for:
  - end-to-end chat through `system:direct` against mock Anthropic
    adaptive, Anthropic non-adaptive, OpenAI reasoning, and Gemini
    reasoning models
  - evaluator path latency and correctness on a reasoning model
  - `test_provider` on a reasoning-capable model
- contract tests for:
  - LiteLLM proxy with `supports_openai_namespace_tools=True`
  - vanilla OpenAI Responses deployment with the flag unset

## Suggested Work Breakdown

### Workstream A: Reasoning and sampling translation

Files likely touched:

- `cognis/providers/llm/reasoning.py`
- `cognis/providers/llm/litellm.py`
- `cognis/models/config.py`

Tasks:

1. Strip `"default"` leakage and implement `"none"` per family.
2. Add provider-layer sampling-param strip for reasoning models.
3. Translate `max_tokens` → `max_completion_tokens` for OpenAI
   reasoning models.
4. Enforce Anthropic `thinking.budget_tokens` vs `max_tokens`
   invariant.

### Workstream B: Auxiliary call-site effort hints

Files likely touched:

- `cognis/core/decision.py`
- `cognis/core/followups.py`
- `cognis/core/step_evaluator.py`
- `cognis/core/compaction.py`

Tasks:

1. Pass explicit low-effort `reasoning_effort` values through each
   auxiliary generate call.
2. Remove hardcoded `temperature=0` where it would conflict with a
   reasoning model.

### Workstream C: Provider resolution and caching

Files likely touched:

- `cognis/providers/llm/litellm.py`

Tasks:

1. Reverse-index cache for `model_id → provider_id`.
2. Deterministic multi-provider resolution rule.
3. Fail-loud on missing provider.
4. Strip `max_retries` from LiteLLM-bound kwargs.
5. Hashed API key in proxy model-info cache key.

### Workstream D: Capability gating and tool exposure

Files likely touched:

- `cognis/providers/llm/litellm.py`
- `cognis/core/tool_exposure.py`
- `cognis/models/config.py`

Tasks:

1. Gate Anthropic `cache_control` on capability flag.
2. Gate Anthropic tool-search beta header on capability flag.
3. Anthropic `disable_parallel_tool_use=false` where supported.
4. Introduce `supports_openai_namespace_tools` and gate namespace
   emission.

### Workstream E: Responses bridge and config

Files likely touched:

- `cognis/providers/llm/responses_bridge.py`
- `cognis/models/agent.py`
- `cognis/core/agent_loop.py`
- `cognis/api/routes/agents.py`
- `cognis/core/workflow_registry.py`

Tasks:

1. Responses bridge: drop missing `tool_call_id`, validate
   `response_format`, correct usage precedence.
2. Surface `agent.llm_config.temperature` into the agent loop.
3. Add `AgentLLMConfig.top_p` and wire into the agent loop.
4. Remove bootstrap defaults that override routing.
5. Validate workflow-step `reasoning_effort` overrides.

### Workstream F: Telemetry and tests

Files likely touched:

- `cognis/providers/llm/litellm.py`
- `cognis/providers/llm/reasoning.py`
- `tests/unit/test_litellm_provider.py`
- `tests/unit/test_tool_exposure.py`
- new unit test modules for reasoning/sampling and resolution
- new integration and contract tests under `tests/integration/` and
  `tests/contract/`

Tasks:

1. Add Prometheus counters.
2. Author unit tests for every translation and gating rule.
3. Author integration tests for end-to-end reasoning behaviour.
4. Author contract tests for LiteLLM proxy and vanilla OpenAI paths.

## Acceptance Criteria

- No provider call carries literal `reasoning_effort="default"`.
- Reasoning-model calls never carry `temperature`, `top_p`, or
  `top_k`.
- Classifier, evaluator, compaction, and follow-up calls set a
  deterministic low-effort reasoning hint.
- Anthropic cache control applies for every
  `supports_prompt_caching=True` model regardless of model name.
- The Anthropic tool-search beta header is sent only when
  `supports_defer_loading=True`.
- OpenAI Responses namespace tools appear only when
  `supports_openai_namespace_tools=True`.
- Turns fail fast and clearly when provider resolution returns `None`.
- Multi-provider resolution is deterministic across runs.
- Proxy model-info cache does not leak across API keys.
- Workflow-step `reasoning_effort` overrides reject typos.
- New Prometheus counters emit values during unit/integration test
  runs.
- Unit, integration, and contract suites are green.
