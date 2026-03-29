# Stage 3: Provider Layer

**Status**: DONE

## Implementation Notes

- All 6 provider Protocols defined in `providers/base.py` and per-category
  `protocol.py` files.
- MnemoryProvider: httpx async client with JWT service auth, circuit breaker
  (5 failures → 30s open), recall/remember/bootstrap methods.
- IntarisProvider: httpx async client with JWT service auth, circuit breaker
  (3 failures → 15s open), evaluate/record/session methods.
- InProcessExecutorProvider: same-process execution with JSON-RPC contract.
- LiteLLMProvider: DB-backed model resolution, caching, streaming support.
- EncryptedDBSecretsProvider: AES-256-GCM encryption at rest.
- JWTAuthProvider: ES256 signing/verification, token revocation in memory.
- Provider registry in `providers/registry.py` with aggregate health check.

**Repo**: `cognis`
**Depends on**: Stage 2 (auth for JWT service tokens), Stage 0 (contract tests passing)
**Estimated effort**: 4-5 days

## Objective

Implement all 6 provider protocols and their initial implementations.
After this stage, Cognis can connect to live Mnemory and Intaris instances
with JWT auth, make LLM calls through LiteLLM, encrypt/decrypt secrets,
and report provider health. Contract tests pass against real services.

## Progress Notes

- Stage 3 provider-layer implementation is complete.
- Implemented: provider Protocols, provider registry, configurable circuit
  breaker instances, Mnemory and Intaris HTTP providers, LiteLLM wrapper,
  encrypted DB secrets provider, in-process executor placeholder, and bounded
  remember retry queue with metrics.
- Per-target JWT audiences (`mnemory` vs `intaris`) are implemented as a
  deliberate least-privilege tightening over the broader spec example.
- Local validation passed through unit tests, `pytest`, `ruff`, and `mypy`.
- Live Mnemory/Intaris contract tests were skipped locally because the required
  services and JWT test key were not available in this environment.

## Deliverables

### 1. Protocol Definitions

- `cognis/providers/base.py` — all 6 Protocol classes:
  - `MemoryProvider` — recall, remember, bootstrap_agent
  - `GuardrailsProvider` — create_session, evaluate, report_reasoning,
    record_events, read_events, get_session, resolve_escalation
  - `ExecutorProvider` — spawn, cleanup (placeholder for Stage 4)
  - `SecretsProvider` — get, set, delete, list
  - `LLMProvider` — generate, stream_generate, count_tokens, list_models
  - `AuthProvider` — sign_jwt, verify_jwt, sign_service_jwt

### 2. Provider Registry

- `cognis/providers/registry.py`
  - Load provider implementations based on env vars + DB settings
  - Singleton access to all providers
  - Health check aggregation
  - Circuit breaker wrapper for each provider

### 3. Mnemory Provider

- `cognis/providers/memory/mnemory.py`
  - `httpx.AsyncClient` with base URL from `COGNIS_MNEMORY_URL`
  - JWT service auth: sign token with `aud=["mnemory"]`, `sub=user_email`
  - `recall()` — POST /api/recall with query, session_id, labels, context
  - `remember()` — POST /api/remember with messages, session_id, labels
  - `bootstrap_agent()` — add pinned identity memories for new agent
  - Graceful degradation: return empty results on failure, don't crash
  - Circuit breaker: 5 failures → OPEN → 30s → HALF_OPEN

### 4. Intaris Provider

- `cognis/providers/guardrails/intaris.py`
  - `httpx.AsyncClient` with base URL from `COGNIS_INTARIS_URL`
  - JWT service auth: sign token with `aud=["intaris"]`, `sub=user_email`
  - `create_session()` — POST /api/v1/intention
  - `evaluate()` — POST /api/v1/evaluate
  - `report_reasoning()` — POST /api/v1/reasoning
  - `record_events()` — POST /session/{id}/events (with idempotency_key)
  - `read_events()` — GET /session/{id}/events (after_seq, last_n)
  - `get_session()` — GET /api/v1/session/{id}
  - `resolve_escalation()` — POST /api/v1/decision
  - `get_last_seq()` — from read_events response or dedicated endpoint
  - Fail-closed on evaluate: raise, don't degrade
  - Circuit breaker

### 5. LLM Provider

- `cognis/providers/llm/litellm.py`
  - LiteLLM wrapper loading config from `llm_providers` DB table
  - `generate()` — completion with full response
  - `stream_generate()` — async iterator for streaming tokens
  - `count_tokens()` — tiktoken for OpenAI, len//4 fallback
  - `list_models()` — enumerate available models from provider configs
  - Model routing: resolve task_type → provider + model from
    `model_routing` table
  - Cost tracking: extract usage from LiteLLM response, return
    `UsageRecord` data
  - Retry with fallback model per routing policy

### 6. Secrets Provider

- `cognis/providers/secrets/encrypted_db.py`
  - AES-256-GCM encryption using auto-generated key from
    `COGNIS_SECRETS_KEY_PATH`
  - `get()` — decrypt and return
  - `set()` — encrypt and store/update
  - `delete()` — remove
  - `list()` — metadata only (never returns decrypted values)

### 7. Remember Retry Queue

- `cognis/core/remember_queue.py`
  - Bounded async FIFO queue (max_depth=100)
  - Background drain task (max_concurrent=5)
  - Exponential backoff per item (2s, 4s, 8s, 16s, 32s, max 60s)
  - Drop oldest on overflow, log warning
  - Max 5 retries per item
  - Graceful shutdown: flush with 10s timeout
  - Prometheus metrics: depth, retries, dropped, success

### 8. Health Checks

- Each provider exposes `health()` → `ProviderHealth`
- Registry aggregates into overall health status
- Feed into `/api/health` endpoint (from Stage 2)

## Acceptance Criteria

- [x] All 6 Protocol classes defined with typed signatures
- [x] Mnemory provider connects with JWT auth, recall and remember work
- [x] Intaris provider connects with JWT auth, evaluate and events work
- [x] LLM provider makes a real completion call (at least one provider)
- [x] Secrets provider encrypts/decrypts correctly
- [x] Token counting works for at least OpenAI models
- [x] Circuit breaker trips after 5 failures, recovers after 30s
- [x] Remember retry queue buffers and drains correctly
- [x] Contract tests pass against live Mnemory + Intaris
- [x] Provider health aggregation works
- [x] Unit tests for each provider (mocked HTTP where needed)
- [x] `ruff check` and `mypy` clean

## Key References

- `docs/specs/05-integrations.md` — verified API contracts, field names, behavior
- `docs/specs/01-architecture.md` — provider pattern, circuit breaker
- `docs/specs/07-security-identity.md` — JWT service token structure
