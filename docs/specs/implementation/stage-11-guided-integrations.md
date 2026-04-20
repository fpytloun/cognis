# Stage 11: Guided Integrations

**Status**: DONE

## Implementation Notes

- Provider presets (OpenAI, Anthropic, Ollama, Custom) with structured
  form fields, model datalists, and base URL handling. "Prefill matching
  secret" button for convenient API key setup.
- Rewrote `POST /api/v1/llm-providers/{id}/test` to perform a real LLM
  completion instead of the stub `"healthy"` response.
- Model routing with autocomplete from configured providers and help text
  explaining each task type.
- MCP server CRUD UI in AgentForm: add/edit/remove servers with name,
  command, args, env vars, and timeout.
- Account management: `POST /api/auth/change-password`, API key CRUD
  endpoints, UI in Settings > Account tab. Removed the "not yet exposed"
  disclaimer from the settings page.
- Contextual secret creation linked from provider setup.

**Repo**: `cognis`
**Depends on**: Stage 10 (launchable first run — UI must be served and setup working)
**Estimated effort**: 4-5 days

## Objective

Replace raw JSON configuration with guided setup for LLM providers, MCP
servers, and account management so a user can get the system working without
reading source code or guessing config formats.

## Context

The current settings UI exposes raw JSON textareas for LLM provider
configuration. The user must know the internal config schema (`default_model`,
`models[]` array with `model_id` and `context_window`). The "Test provider"
button calls `providers.llm.health()` which unconditionally returns
`"healthy"` — it never actually tests the provider. MCP server configuration
has no UI at all (API-only via the agent's `tools` JSON blob). Password
change and API key management are acknowledged as missing in the settings
page.

## Deliverables

### 1. LLM Provider Presets

Replace the raw JSON textarea with structured forms for common providers.

- **Preset selector**: dropdown at the top of the provider form with
  options: OpenAI, Anthropic, Ollama, Custom (raw JSON).
- **Per-preset form fields**:
  - **OpenAI**: API key (password field), base URL (optional, for proxies),
    default model (dropdown: gpt-4o, gpt-4o-mini, o3-mini, etc.),
    additional models (add/remove list).
  - **Anthropic**: API key, default model (claude-sonnet-4-20250514,
    claude-haiku, etc.).
  - **Ollama**: base URL (default `http://localhost:11434`), default model
    (free text, with common suggestions), no API key needed.
  - **Custom**: raw JSON textarea (current behavior) for any LiteLLM-
    supported provider.
- **Form → JSON mapping**: the structured form fields are converted to the
  internal config JSON on save. The JSON is still the storage format — the
  form is a UI layer on top.
- **Edit existing**: when editing a provider, detect the preset from the
  config shape and show the structured form. Fall back to raw JSON if the
  config does not match a known preset.
- **Client-side validation**: required fields (API key for OpenAI/Anthropic,
  base URL for Ollama), model name format, JSON syntax for custom mode.

### 2. Meaningful Provider Test

Replace the stub health check with a real end-to-end provider test.

- **Backend** (`cognis/api/routes/settings.py:157`): rewrite the
  `POST /api/v1/llm-providers/{id}/test` endpoint to:
  1. Load the provider config from DB.
  2. Resolve the default model via `LiteLLMProvider.resolve_model()`.
  3. Attempt a minimal completion: `litellm.acompletion(model=resolved,
     messages=[{"role": "user", "content": "Say hello."}], max_tokens=5)`.
  4. Return `{ok: bool, model_resolved: str, latency_ms: int,
     error_detail: str | null}`.
- **Backend** (`cognis/providers/llm/litellm.py:148`): replace the stub
  `health()` method with a real check that attempts model resolution and
  optionally a test completion.
- **UI**: show the test result inline — green check with model name and
  latency on success, red error with the actual failure message (auth
  failure, model not found, rate limit, timeout, connection refused, etc.).
- **Timeout**: 15-second timeout on the test completion to avoid hanging.

### 3. Model Routing Guidance

Make model routing understandable and easy to configure.

- **Model picker**: replace free-text model inputs with a
  dropdown/autocomplete that lists models from all configured providers
  (sourced from provider config `models[]` arrays).
- **One-click default**: "Use default model for all task types" button
  that copies the default model to all routing slots.
- **Help text**: explain what each task type does:
  - `default` — main chat and task execution
  - `classifier` — decision engine (fast model recommended)
  - `compaction` — context compaction summaries
- **Validation**: warn if a routing slot references a model not found in
  any configured provider.

### 4. MCP Server Configuration UI

Add MCP server management to the agent form.

- **New section in `AgentForm.svelte`**: "MCP Servers" card between
  "Tools & permissions" and "LLM config".
- **Server list**: shows configured MCP servers with name, command, status.
- **Add/edit form**: name (required), command (required), args (array,
  one per line or comma-separated), environment variables (key=value list),
  timeout seconds (default 30).
- **Remove**: delete button per server with confirmation.
- **Test button**: `POST /api/v1/agents/{id}/mcp/test` (new endpoint).
  Backend spawns the MCP server process, runs `initialize` + `tools/list`,
  returns discovered tool names or error. UI shows pass/fail with tool
  count or error detail.
- **Tool discovery**: after saving an agent with MCP servers, the tool
  permission table should include the discovered MCP tools (fetched via
  the existing `GET /api/v1/agents/{id}/tools` endpoint after a brief
  delay or manual refresh).
- **Storage**: MCP servers are stored in the agent's `tools.mcp_servers`
  JSON field (existing schema, no migration needed).

### 5. Account Management

Add password change and API key management to the UI.

- **Password change**:
  - New endpoint: `POST /api/auth/change-password` accepting
    `{current_password, new_password}`. Validates current password via
    argon2id, hashes new password, updates DB.
  - UI: form in Settings > Account tab with current password, new password,
    confirm new password fields. Client-side validation (min length, match).
- **API key management**:
  - New endpoints:
    - `GET /api/v1/auth/api-keys` — list current user's API keys
      (key_id, name, prefix, created_at, last_used_at).
    - `POST /api/v1/auth/api-keys` — create new key, return full key
      once (not stored in plaintext).
    - `DELETE /api/v1/auth/api-keys/{key_id}` — revoke key.
  - UI: API key list in Settings > Account tab with create button (shows
    key once in a copy-able field), revoke button per key.
- **Remove the "not yet exposed" note** from the settings page once both
  features are implemented.

### 6. Secrets UX Improvements

Make the relationship between secrets, providers, and tools clearer.

- **Contextual links**: when creating an LLM provider that needs an API
  key, show a note: "LiteLLM reads API keys from environment variables.
  Set `OPENAI_API_KEY` before starting Cognis, or add it as a secret for
  tool execution sandboxes."
- **Help text**: explain the difference between env-var API keys (read by
  LiteLLM at runtime) and encrypted secrets (injected into executor
  sandboxes for tool use).
- **Pre-filled secret creation**: "Add as secret" link from the provider
  form that pre-fills the secret name (e.g., `openai_api_key`) and scope.

## Acceptance Criteria

- [x] User can create an OpenAI provider using structured form fields
      (no JSON required)
- [x] User can create an Anthropic provider using structured form fields
- [x] User can create an Ollama provider using structured form fields
- [x] Custom/advanced mode still supports raw JSON
- [x] Provider test reports real pass/fail with model name, latency, and
      actionable error details
- [x] Model routing shows available models from configured providers
- [x] Model routing has help text explaining each task type
- [x] MCP servers can be added, edited, and removed from the agent form
- [x] MCP server test discovers tools and reports results
- [x] Discovered MCP tools appear in the agent's tool permission table
- [x] Password change works from Settings > Account
- [x] API keys can be created, listed, and revoked from Settings > Account
- [x] "Not yet exposed" note is removed from the settings page
- [x] Secrets have contextual help explaining env vars vs encrypted secrets

## Key References

- `cognis/api/routes/settings.py:157` — current stub provider test
- `cognis/providers/llm/litellm.py:148` — stub health method
- `cognis/providers/llm/litellm.py:33` — model resolution logic
- `ui/src/routes/(app)/settings/+page.svelte` — settings UI
- `ui/src/lib/components/agents/AgentForm.svelte` — agent form (MCP target)
- `cognis/api/routes/tools.py` — MCP server discovery endpoint
- `cognis/api/routes/auth.py` — auth endpoints (password change target)
- `cognis/security.py` — password hashing, API key generation
- `cognis/store/models.py` — api_keys table (already exists)
- `docs/specs/10-api-spec.md` — API surface reference
