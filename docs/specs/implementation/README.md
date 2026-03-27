# Cognis Implementation Stages

## Progress Tracker

| Stage | Name | Status | Notes |
|-------|------|--------|-------|
| 0 | [Prerequisites](stage-0-prerequisites.md) | IN PROGRESS | Local implementation underway across Intaris, Mnemory, and Cognis contract tests |
| 1 | [Project Scaffold](stage-1-scaffold.md) | IMPLEMENTED* | Locally validated; direct Alembic CLI execution still needs manual verification |
| 2 | [Auth + Bootstrap + CLI](stage-2-auth.md) | IMPLEMENTED* | Locally validated; full live runtime/WS verification remains environment-dependent |
| 3 | [Provider Layer](stage-3-providers.md) | IMPLEMENTED* | Locally validated; live Mnemory/Intaris contract checks skipped without services |
| 4 | [Executor + Tools](stage-4-executor.md) | IMPLEMENTED* | In-process executor, MCP, tool router; remote executor WS deferred to Stage 7 |
| 5 | [Orchestration Core](stage-5-orchestration.md) | NOT STARTED | Session manager, cache, context, compaction |
| 6 | [Agent Loop + Delegation](stage-6-agent-loop.md) | NOT STARTED | Chat turns, streaming, delegation |
| 7 | [API + WebSocket](stage-7-api.md) | NOT STARTED | Full REST + WS surface |
| 8 | [UI](stage-8-ui.md) | NOT STARTED | SvelteKit: chat, agents, settings |
| 9 | [Integration Testing](stage-9-testing.md) | NOT STARTED | Full flow tests, polish |

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
         Stage 6 (agent loop + delegation)
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
