# Cognis Implementation Stages

## Progress Tracker

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
| 9 | [Integration Testing](stage-9-testing.md) | IN PROGRESS | Integration infra + 16 passing tests + 3 bug fixes; WS/LLM chat tests need live server (Phase 2) |

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
```

Stages 4 and 5 can run in parallel after Stage 3 is complete.

## How To Use This

1. Before starting a stage, read its file for scope, deliverables, and
   acceptance criteria.
2. When a stage is in progress, update its status in this table.
3. When a stage is done, mark it DONE and note any deviations or
   follow-ups.
4. Reference the relevant specs from `docs/specs/` for detailed design.
