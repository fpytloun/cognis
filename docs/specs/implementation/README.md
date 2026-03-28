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
| 9 | [Integration Testing](stage-9-testing.md) | IN PROGRESS | Full integration suite passing (27 tests) + contract refresh passing (14 tests, 2 skipped); manual shutdown/compaction coverage still pending |

### MVP Polish (Stages 10-15)

| Stage | Name | Status | Notes |
|-------|------|--------|-------|
| 10 | [Launchable First Run](stage-10-first-run.md) | DONE | Bundled UI serving, setup page, readiness banner, startup diagnostics, Dockerfile |
| 11 | [Guided Integrations](stage-11-guided-integrations.md) | DONE | Provider presets/tests, model routing guidance, MCP management, account management |
| 12 | [Honest Bootstrap + Docs](stage-12-bootstrap-docs.md) | DONE | Rewritten Quick Start, in-app getting-started guide, diagnostics, user docs |
| 13 | [Core UX Polish](stage-13-ux-polish.md) | PENDING | Toasts, validation, confirmations, chat shortcuts, icons, accessibility, mobile |
| 14 | [Degraded Mode + Recovery UX](stage-14-degraded-ux.md) | PENDING | Outage banners, contextual errors, setup-incomplete states, retry affordances |
| 15 | [MVP Closure Sweep](stage-15-closure.md) | PENDING | Backend TODOs, skills loader, rate limiting, test coverage, tracker alignment |

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
```

Stages 4 and 5 can run in parallel after Stage 3 is complete.
Stages 12 and 13 can run in parallel after Stage 11 is complete.

## Scope Boundary

Stages 0-9 build the MVP. Stages 10-15 polish it into a usable product.

**Out of scope for all stages** (Phase 2+): multi-user, scheduler
execution, Docker/K8s executors, platform integrations (Slack/Discord),
A2A federation, cost tracking dashboard, interactive CLI chat, Redis L2
cache, PostgreSQL migration tooling, agent export/import YAML.

## How To Use This

1. Before starting a stage, read its file for scope, deliverables, and
   acceptance criteria.
2. When a stage is in progress, update its status in this table.
3. When a stage is done, mark it DONE and note any deviations or
   follow-ups.
4. Reference the relevant specs from `docs/specs/` for detailed design.
