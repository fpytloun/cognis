# Cognis Implementation Stages

This tracker is internal implementation history. For current user-facing behavior, start with the repository README and `docs/guide/`.

## Progress Tracker

### MVP Build (Stages 0-9)

| Stage | Name | Status | Notes |
|-------|------|--------|-------|
| 0 | [Prerequisites](stage-0-prerequisites.md) | DONE | Intaris I1-I6, Mnemory M1, contract tests |
| 1 | [Project Scaffold](stage-1-scaffold.md) | DONE | Package, config, DB schema, Alembic, ~/.cognis |
| 2 | [Auth + Bootstrap + CLI](stage-2-auth.md) | DONE | JWT, JWKS, setup URL, Typer CLI |
| 3 | [Provider Layer](stage-3-providers.md) | DONE | All 6 protocols + implementations |
| 4 | [Executor + Tools](stage-4-executor.md) | DONE | In-process executor, MCP, tool router |
| 5 | [Orchestration Core](stage-5-orchestration.md) | DONE | Session manager, cache, context, compaction, decision engine |
| 6 | [Agent Loop + Workflow Engine](stage-6-agent-loop.md) | DONE | Step runner, workflow engine, evaluator, task queue, gates, event bus, registry |
| 7 | [API + WebSocket](stage-7-api.md) | DONE | REST + WS MVP surface, cursor pagination, sessions routes, task controls, reconnect support |
| 8 | [UI](stage-8-ui.md) | DONE | SvelteKit UI under `ui/` with auth, chat, agents, tasks, workflows, settings, and Stage 8 workflow API alignment |
| 9 | [Integration Testing](stage-9-testing.md) | DONE | 27+ integration tests, 14 contract tests, 21 WebSocket unit tests, compaction/shutdown/recovery/degradation tests, all MVP success criteria verified |

### MVP Polish (Stages 10-16)

| Stage | Name | Status | Notes |
|-------|------|--------|-------|
| 10 | [Launchable First Run](stage-10-first-run.md) | DONE | Bundled UI serving, setup page, readiness banner, startup diagnostics, Dockerfile |
| 11 | [Guided Integrations](stage-11-guided-integrations.md) | DONE | Provider presets/tests, model routing guidance, MCP management, account management |
| 12 | [Honest Bootstrap + Docs](stage-12-bootstrap-docs.md) | DONE | Rewritten Quick Start, in-app getting-started guide, diagnostics, user docs |
| 13 | [Core UX Polish](stage-13-ux-polish.md) | DONE | Global toasts/confirmations, mobile shell, keyboard shortcuts, chat search/pagination/timestamps, unsaved-change guards |
| 14 | [Degraded Mode + Recovery UX](stage-14-degraded-ux.md) | DONE | Provider outage banners, contextual chat failures, setup-incomplete chat/task states, retry affordances, sync warnings |
| 15 | [MVP Closure Sweep](stage-15-closure.md) | DONE | Delivery/purge TODOs resolved, skills loader MVP, general API rate limiting, sync metadata, expanded unit/UI coverage |
| 16 | [Executor-Native Tools, Tool Management UI, and Executor UI](stage-16-tools-executors.md) | DONE | Native executor tools, Tools & Skills workspace, executor CRUD/tool toggles, default executor bootstrap |

## Dependency Graph

```
Stage 0 (prerequisites — Intaris + Mnemory repos)
  │
  ▼
Stage 1 (scaffold + DB)
  │
  ▼
Stage 2 (auth + CLI)
  │
  ▼
Stage 3 (providers) ──────────────┐
  │                                │
  ▼                                ▼
Stage 4 (executor + tools)    Stage 5 (orchestration core)
  │                                │
  └──────────┬─────────────────────┘
             │
             ▼
         Stage 6 (agent loop + workflow engine — core)
             │
             ▼
         Stage 7 (API + WebSocket)
             │
             ▼
         Stage 8 (UI)
             │
             ▼
         Stage 9 (testing + polish)
             │
             ▼
         Stage 10 (launchable first run)
             │
             ▼
         Stage 11 (guided integrations)
             │
             ├──────────────────┐
             ▼                  ▼
         Stage 12 (docs)    Stage 13 (UX polish)
             │                  │
             └────────┬─────────┘
                      ▼
                  Stage 14 (degraded mode + recovery UX)
                      │
                      ▼
                   Stage 15 (MVP closure sweep)
                      │
                      ▼
                   Stage 16 (executor tools + tools/executors UI)
```

Stages 4 and 5 can run in parallel after Stage 3 is complete.
Stages 12 and 13 can run in parallel after Stage 11 is complete.

### Spec Alignment (Post-MVP)

Specs have evolved since the MVP build with significant new design:
workflow step input context model, task delivery routing, step questions,
collaboration/sharing model, and channel-aware conversations. These stages
bring the codebase in line with the updated specs.

| Stage | Name | Status | Notes |
|-------|------|--------|-------|
| 6a | [Step Input Context Assembly](stage-6a-workflow-context.md) | DONE | null/full/summary/last input types, iteration semantics, step output storage, same-session retry |
| 20 | [Harness Correctness and Concurrency Stabilization](stage-20-harness-correctness.md) | DONE | Remove shared singleton turn state, bound retry/wait loops, fix gate restart semantics, harden memory identity, preserve transcript integrity |
| 21 | [Harness Capability Parity](stage-21-harness-capability.md) | DONE | Parallel read-only tools, `ripgrep`/`fd` search, background shells, finish-reason handling, token-aware truncation, step timeouts |
| 22 | [Harness Prompt Cache and Operational Resilience](stage-22-harness-operations.md) | DONE | Immutable-prefix cleanup, cache-stable tool exposure, durable remember queue, multi-replica dedupe, Intaris breaker isolation (skill-load context protection completed in Stage 25) |
| 23 | [Provider and Model Handling Stabilization](stage-23-provider-stabilization.md) | DONE | Reasoning-effort translation, sampling-parameter stripping, `max_tokens`→`max_completion_tokens`, capability-flag gating for cache/beta headers, deterministic provider resolution, Responses bridge hardening |
| 25 | [Harness Polish and Remaining Gap Fill](stage-25-harness-polish.md) | DONE | MCP image/resource passthrough, skill-load context protection, mid-stream recovery reversal, provider-native tokenizer, session-lock sweeper, EventBus dead-subscriber eviction, dynamic MCP nonexistent-tool prompt, workflow `reasoning_effort` validator |
| 26 | [LLM-Exposure Auditing in Intaris](stage-26-llm-exposure-audit.md) | PLANNED | New `system_message`/`developer_message`/`context_snapshot` Intaris events, full per-turn LLM-exposure audit, Intaris-anchored immutable prefix, removal of `memory_stale` and per-field TTL, hard-fail on missing core memories |
| 27 | [Browser Takeover and Session Recording](stage-27-browser-takeover-recording.md) | PLANNED | Optional noVNC browser takeover, Intaris-owned recording/evidence flow, browser/desktop audit model |
| 28 | [First-Class Agent Runtimes](stage-28-agent-runtimes.md) | PLANNED | Runtime abstraction, `runtime_runs`, executor runtime RPC, Claude runtime host, projection model, direct chat/delegation/workflow parity |
| 29 | [Agent Sharing](stage-29-agent-sharing.md) | DONE | User-to-user agent sharing, `agent_grants` table, two-headed runtime identity (acting user + agent owner), Mnemory `(user, owner)` keying, owner-configurable executor scope per share, no admin bypass for user-owned resources |
| 30 | [Auto Routing for Agents and Workflows](stage-30-auto-routing.md) | PLANNED | `auto` / `self` routing semantics, shared routing helper, execution-envelope enforcement, classifier fallback, telemetry |
| 31 | [Workflow Deliverables and Step Profiles](stage-31-workflow-deliverables.md) | DONE | Typed deliverables + `write_deliverable`, once-only channel delivery, step profiles (`research`/`coding`), tool classification, system workflow wiring |
| 32 | [Workflow-First Composition and Ephemeral Workflows](stage-32-workflow-composition.md) | DONE | `compose_and_run_workflow`, hidden workflow-composer and skill-decomposer agents, ephemeral workflow lifecycle, coding workflow family, skill `steps:` extension, promote-from-task UX |
| 33 | [Projects, Step Metadata Gating, and Human-as-Evaluator Revisions](stage-33-projects-and-revisions.md) | DONE | Projects with multi-source repos and shareable grants, project-aware tasks/schedules/conversations, project-bound workflow eligibility, path-touch project context injection, step-completion metadata contracts, conditional gate DSL, task comments with intent, human-driven revisions with classifier-selected re-entry steps and preserved step-run history |
| 34 | [Voice Mode (TTS, STT, Conversation Mode)](stage-34-voice-mode.md) | DONE | `LLMProvider.synthesize()` and executor routing, `text_to_speech` model routing slot, `tts_cache` table, `POST /api/v1/tts/synthesize` and `POST /api/v1/stt/transcribe`, per-agent voice with system fallback, speaker button on assistant messages, web microphone with iMessage-style record-preview-send (STT-first), sentence-buffered TTS streaming, and bidirectional conversation-mode overlay |
| 35 | [Conversation Search](stage-35-conversation-search.md) | PLANNED | Intaris-owned hybrid search: lexical Tier 1 (PG `tsvector` + `pg_trgm`, SQLite FTS5 trigram) mandatory, vector Tier 2 (pgvector / Qdrant URL or local-mode) optional, outbox-backed indexer, `INTARIS_SEARCH_ENABLED` feature flag (default `true`), `/api/v1/search/*` endpoints + Intaris UI. Cognis proxy + join with `conversations`, in-conversation Cmd+F with magnifier and client-first/server-fallback, sidebar promote-to-search with explicit submit, three LLM tools (`list_conversations`, `search_conversations`, `read_conversation_messages` with anchor-based pagination), `conversation_id` propagated through `RuntimeAccessContext` and tool runtime metadata, strict user scoping (no admin bypass, no agent-grant expansion). |

## Scope Boundary

Stages 0-9 build the MVP. Stages 10-16 polish it into a usable product.
Stages 6a+ align the codebase with spec refinements made during design review.
Stages 20-23 are the harness and provider stabilization sequence. Stage 25 is
the final harness polish pass that closes verified gaps remaining after the
stabilization sweep. Stage 26 remains planned. Browser takeover, first-class
agent runtimes, and auto routing remain deferred design work, while agent
sharing, workflow deliverables, step profiles, workflow composition, and
projects are represented in current code and user-facing guides.

Stage 33 sits on top of stages 28–32 because it depends on the deliverable
contract, step profiles, agent sharing primitives, and workflow-first
composition substrate. Its phases (33.1–33.11) are individually mergeable
but ship under one tracker entry.

Stage 34 is independent of stages 28–33 and adds voice mode end-to-end:
TTS provider plumbing, the web mic flow, speaker buttons, sentence-buffered
streaming, and a dedicated conversation-mode overlay. Its phases (34.1–34.9)
are individually mergeable.

Stage 35 is independent of stages 28–34 and adds conversation search across
Intaris and Cognis. The bulk of the work (35.1–35.5) lands in Intaris and
can be reviewed without Cognis. The Cognis-side phases (35.6–35.8) can be
drafted in parallel once the Intaris API shape is stable. All phases are
individually mergeable.

**Still out of scope / not yet shipped**: multi-user production hardening,
Docker/K8s executors, A2A federation, cost tracking dashboard,
interactive CLI chat, Redis L2 cache, and PostgreSQL migration tooling.

## How To Use This

1. Before starting a stage, read its file for scope, deliverables, and
   acceptance criteria.
2. When a stage is in progress, update its status in this table.
3. When a stage is done, mark it DONE and note any deviations or
   follow-ups.
4. Reference the relevant specs from `docs/specs/` for detailed design.
