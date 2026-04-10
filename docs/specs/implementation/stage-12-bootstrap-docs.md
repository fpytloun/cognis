# Stage 12: Honest Bootstrap and Documentation

**Status**: DONE

## Implementation Notes

- Rewrote README.md Quick Start with honest step-by-step guide covering
  all three services, JWT key sharing, and LLM API key setup.
- In-app getting-started wizard with progress tracking (localStorage),
  auto-detection of completed steps via diagnostics, and links to all
  6 user guide documents.
- Diagnostics in Settings > System: service connectivity, JWT key
  fingerprint, DB status, "Copy env block" button.
- All 6 user guides in `docs/guide/`: getting-started, configuring-providers,
  creating-agents, using-chat, managing-tasks, troubleshooting. Linked
  from the getting-started page (GitHub blob URLs).
- README updated: removed "zero-config" language, documented bundled UI
  and Docker deployment, accurate feature list.

**Repo**: `cognis`
**Depends on**: Stage 11 (guided integrations — docs should describe the guided config, not the raw JSON interim)
**Estimated effort**: 2-3 days

## Objective

Align all documentation, in-app text, and startup behavior with the actual
product capabilities. No claim should exceed reality. After this stage, a
new user can follow the docs end-to-end on a clean machine and reach a
working system.

## Context

The current README describes a "zero-config local deployment" that is not
zero-config in practice — it requires three services, environment variables
for LLM API keys, and manual provider configuration. There is no user-facing
documentation beyond the developer-oriented README. The in-app experience
provides no onboarding guidance after the initial setup.

Stages 10 and 11 fix the actual product gaps. This stage ensures the
documentation and in-app guidance accurately describe the improved product.

## Deliverables

### 1. Rewritten Quick Start

Replace the current README Quick Start with an honest, step-by-step guide.

- **Prerequisites**: Python 3.12+, an LLM API key (OpenAI, Anthropic, or
  local Ollama).
- **Step 1 — Start companion services**:
  ```bash
  uvx mnemory                     # Memory on :8050
  uvx intaris                     # Guardrails on :8060
  ```
  Explain what each service does in one sentence. Note that Mnemory and
  Intaris need Cognis's public key for JWT validation (with copy-paste
  commands).
- **Step 2 — Start Cognis**:
  ```bash
  OPENAI_API_KEY=sk-... uvx cognis-controller
  ```
  Show the API key as part of the command. Explain that Cognis creates
  `~/.cognis/` with auto-generated keys and a SQLite database.
- **Step 3 — Complete setup**: open the printed URL, create admin account
  via the setup form.
- **Step 4 — Configure LLM provider**: navigate to Settings > Providers,
  use the preset form.
- **Step 5 — Create an agent**: navigate to Agents > New, fill in basics.
- **Step 6 — Chat**: start a conversation.
- **Troubleshooting section**: common failures (Intaris unreachable,
  LLM auth error, setup token expired) with resolution steps.

### 2. In-App Setup Guide

Expand the readiness checklist from Stage 10 into a step-by-step wizard.

- **Wizard flow**: after first login, show a guided setup overlay or
  dedicated `/getting-started` page with numbered steps:
  1. Check companion services (auto-detected from health endpoint)
  2. Configure LLM provider (link to Settings > Providers)
  3. Create your first agent (link to Agents > New)
  4. Start chatting (link to Chat > New)
- **Progress tracking**: each step shows done/pending status. Persisted
  in localStorage (or a user setting) so it survives page reloads.
- **Dismissible**: "Skip setup guide" button. Re-accessible from
  Settings > System or a help menu.
- **Contextual**: steps that are already complete (e.g., provider exists)
  are auto-marked as done.

### 3. Readiness Diagnostics Page

Dedicated diagnostics view for operators and troubleshooting.

- **Location**: Settings > System tab, expanded section or dedicated
  `/settings/diagnostics` route.
- **Content**:
  - Service connectivity: Mnemory URL + status, Intaris URL + status,
    LLM provider status (from `/api/health`).
  - JWT key status: public key path, key fingerprint, whether companion
    services accept the key.
  - Database status: engine type (SQLite/PostgreSQL), migration version.
  - Configuration summary: `COGNIS_DATA_DIR`, `COGNIS_PORT`,
    `COGNIS_SERVE_UI`, companion service URLs.
  - Configured providers: list with last test result.
  - Active agents: count and names.
- **Copy-paste env block**: button that copies the current environment
  configuration as a shell export block for sharing or reproduction.
- **Links**: each diagnostic item links to the relevant settings page
  or documentation section.

### 4. Updated README.md

Revise the README to accurately describe the polished product.

- **Remove "zero-config" language** where it implies no setup is needed.
  Replace with "quick local bootstrap" or "minimal setup".
- **Document bundled UI**: explain that `uvx cognis-controller` serves the web UI
  on `:8080` by default, with `COGNIS_SERVE_UI=false` for split
  deployments.
- **Document Docker deployment**: single image, combined vs API-only
  modes, example docker-compose with all three services.
- **Accurate feature list**: do not claim features that are Phase 2
  (Docker/K8s executors, platform integrations, interactive CLI chat).
- **Development section**: keep the `npm run dev` workflow for UI
  development, clarify it is not needed for end users.

### 5. User-Facing Documentation

Create a `docs/guide/` directory with user documentation.

- **`getting-started.md`**: mirrors the Quick Start but with more detail,
  screenshots, and alternative paths (Ollama, Docker).
- **`configuring-providers.md`**: how to add LLM providers (presets and
  custom), model routing, testing connectivity.
- **`creating-agents.md`**: agent identity fields, personality, tool
  permissions, MCP servers, workflow settings.
- **`using-chat.md`**: sending messages, streaming, tool calls, delegation
  cards, escalation prompts, reconnection.
- **`managing-tasks.md`**: task board, creating drafts, batch submit,
  dependencies, workflow progress, gates.
- **`troubleshooting.md`**: common errors, provider failures, JWT issues,
  setup token expiry, companion service problems.
- **Linked from UI**: help icon in the app header or settings page that
  opens the docs (served as static files or linking to a docs site).

## Acceptance Criteria

- [x] A new user can follow the Quick Start end-to-end on a clean machine
      and reach a working chat
- [x] In-app setup guide walks through all required configuration steps
- [x] Setup guide tracks completion and is re-accessible
- [x] Diagnostics page shows all service/config status
- [x] Diagnostics page provides copy-paste env block
- [x] README accurately describes the current product capabilities
- [x] README documents bundled UI and Docker deployment
- [x] User guide covers: getting started, providers, agents, chat, tasks,
      troubleshooting
- [x] No documentation claims features that do not exist
- [x] Docs are linked from the UI

## Key References

- `README.md` — current README (needs revision)
- `docs/specs/11-deployment.md` — deployment spec
- `docs/specs/12-mvp-roadmap.md` — MVP scope and success criteria
- `cognis/api/routes/system.py` — health endpoint (diagnostics data source)
- Stage 10 deliverable 3 — readiness checklist (expanded here into wizard)
