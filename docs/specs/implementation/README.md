# Cognis Implementation Stages

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

## Scope Boundary

Stages 0-9 build the MVP. Stages 10-16 polish it into a usable product.
Stages 6a+ align the codebase with spec refinements made during design review.

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
