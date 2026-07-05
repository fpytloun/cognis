# Spec 33 — Deterministic E2E Streaming-Chat Test Harness + Per-Agent Backend Registry

## Status: In implementation

## Overview

This spec covers two tightly coupled features:

1. **Per-agent backend registry** — a pluggable, community-extensible system for selecting
   memory and guardrails backends per agent (e.g. `mnemory|none`, `intaris|none`, future
   `native`). Enables LLM-free agent configurations for testing and future backend diversity.

2. **Deterministic E2E test harness** — a three-layer test pyramid (unit → golden
   event-stream → browser) backed by a deterministic mock LLM provider and a shared
   compose stack. Supports both static CI tests and interactive agent-driven debugging.

---

## Background and motivation

The timeline refactor (spec 32 / commit f6d42264) exposed a class of streaming-chat bugs
(hanging spinners, appear/disappear events, duplicate items) that are caused by ordering
races in the WebSocket event stream. These bugs are:

- **Non-deterministic** with real LLM providers — cannot be reliably reproduced.
- **Invisible to unit tests** — they require a real backend + real WS stream.
- **Invisible to browser tests** without a deterministic model — real providers produce
  different token sequences each run.

The solution is a **deterministic mock LLM provider** that replays exact scripted sequences,
combined with a per-agent capability system that lets e2e agents run without Mnemory/Intaris
LLM dependencies.

---

## Part A — Per-agent backend registry

### A1. Backend registry (`cognis/providers/backends/`)

A discovery-based registry where backends are self-registering modules. Each backend module
exposes a descriptor and registers via a decorator. Community backends can be added by
dropping a module and registering — no core edits required.

```
cognis/providers/backends/
├── __init__.py          # Registry + decorator + resolve_agent_backends()
├── memory/
│   ├── __init__.py
│   ├── mnemory.py       # backend_id="mnemory", wraps MnemoryProvider
│   └── null.py          # backend_id="none", NullMemoryProvider
└── guardrails/
    ├── __init__.py
    ├── intaris.py       # backend_id="intaris", wraps IntarisProvider
    └── null.py          # backend_id="none", NoGuardrailsProvider
```

Each backend module:
```python
@register_backend(kind="memory", id="none")
def _factory(config: CognisConfig, registry: ProviderRegistry) -> MemoryProvider:
    return NullMemoryProvider()
```

`resolve_agent_backends(agent, config, registry)` returns `(memory, guardrails)` effective
providers for a turn.

### A2. AgentCapabilities model

```python
class AgentCapabilities(BaseModel):
    memory_backend: str = "mnemory"
    guardrails_backend: str = "intaris"
```

Added to `AgentDefinition.capabilities` with `default_factory=AgentCapabilities`.
String-typed (not Literal) so new backends need no schema change. Validated against the
registry at parse time with a clear error for unknown values.

### A3. Persistence

- `capabilities` JSON column on `Agent` ORM (nullable, default `{}`).
- **Both** an Alembic migration (`store/migrations/versions/`) **and** an idempotent
  `_ensure_agent_capabilities_column()` in `bootstrap.py` (AGENTS.md dual-mechanism rule).
- Parsed in `_row_to_definition()` (agent_registry.py) and the override/effective paths.
- Serialized in `create_agent`/`update_agent` query helpers.

### A4. Runtime gating

**Memory = `none`** (`NullMemoryProvider`):
- `context.py`: skip `self.memory.recall(...)` → return empty recall result.
- `agent_loop.py`: skip `remember()` / `add_memory` background calls.
- Agent bootstrap: skip `bootstrap_agent`/identity replace.

**Guardrails = `none`** (`NoGuardrailsProvider`):
- `tool_router.py` (both call sites ~454, ~494): skip `guardrails.evaluate()` → return
  `PermissionDecision(decision="approve", source="capability-disabled")`.
- **This includes non-bypassable tools** — `none` means no guardrails, period.
- `agent_loop.py` (~14118): skip `report_reasoning()`.
- Intaris event store (`record_events`/`read_events`/`create_session`) is **always used**
  regardless of guardrails backend — it is the session content store, not a guardrails
  feature.

`NoGuardrailsProvider` is a decorator wrapping `IntarisProvider`: passes through all
storage operations, short-circuits `evaluate`→approve and `report_reasoning`→noop.

### A5. Config defaults

New env vars in `CognisConfig`:
- `COGNIS_DEFAULT_MEMORY_BACKEND` (default: `"mnemory"`)
- `COGNIS_DEFAULT_GUARDRAILS_BACKEND` (default: `"intaris"`)

Used as the fallback when an agent omits `capabilities`. E2E compose stack sets both to
`"none"` so all seeded agents run LLM-free by default.

### A6. API + UI + contract tests

- `capabilities` in agent create/update API request/response models (`api/models.py`).
- `AgentCapabilities` in `ui/src/lib/types/api.ts` (enforced by `test_ui_contract_sync.py`).
- Agent editor UI: capabilities section (memory backend selector, guardrails backend
  selector) — hidden/disabled in UI for backends not yet production-ready.

---

## Part B — Deterministic Mock LLM provider

### B1. `cognis/testing/mock_llm/`

A standalone Starlette app implementing OpenAI-compatible endpoints:
- `POST /v1/chat/completions` — stream + non-stream, replays scripted SSE.
- `POST /v1/responses` — Responses API (for Codex/GPT-5 paths).
- `POST /v1/embeddings` — deterministic fixed-dim vectors (for Mnemory/Intaris-search).

Runnable as:
- `python -m cognis.testing.mock_llm` (compose service)
- In-process pytest fixture (subprocess on a free port)

### B2. Control plane (enables interactive debugging)

Admin endpoints on the same server:
- `POST /__mock/scenario` — inject/override a scenario at runtime.
- `POST /__mock/active` — set which scenario the next turn uses.
- `GET /__mock/scenarios` — list loaded scenarios.
- `GET /__mock/history` — last N request/response pairs (for debugging).

Scenario keying: last-user-message marker (e.g. `"scenario:multiphase-thinking-tool"`)
**or** the `active` override set via `/__mock/active`. This makes the same server serve
both static tests (fixture files) and interactive debugging (agent-injected scripts).

### B3. Scenario script format (`tests/e2e/scenarios/*.yaml`)

```yaml
id: thinking-multiblock
description: "Thinking segment grows a second block mid-stream (id-mutation bug)"
trigger: "scenario:thinking-multiblock"
turns:
  - role: assistant
    steps:
      - type: thinking
        block_id: blk_1
        title: "Thinking"
        content: "Step 1 reasoning"
        complete: false
        delay_ms: 50
      - type: thinking
        block_id: blk_1
        title: "Thinking"
        content: "Step 1 reasoning (complete)"
        complete: true
        delay_ms: 50
      - type: thinking
        block_id: blk_2
        title: "Thinking"
        content: "Step 2 reasoning"
        complete: false
        delay_ms: 50
      - type: text
        chunks: ["Here ", "is ", "my ", "answer."]
        delay_ms: 30
```

### B4. Scenario catalog (7 scenarios, 1:1 with bug classes)

1. `single-phase-stream` — baseline: stream text, complete. Assert spinner clears.
2. `thinking-multiblock` — thinking segment grows a second block mid-stream (id-mutation
   hanging-spinner bug).
3. `multiphase-thinking-tool-assistant` — thinking → tool_call → assistant (phase bump);
   assert all phases finalize, no hanging phase-0 spinner.
4. `tool-args-then-result` — on_tool_call(args) then tool_result(no args); assert
   arguments preserved (title not lost from collapsed card).
5. `escalation-after-tool` — tool_call then escalation event; assert badge appears, no
   ghost/duplicate.
6. `rapid-tokens` — high-rate tokens; assert single render path, no flicker, monotonic
   id-set.
7. `refresh-mid-and-post-turn` — uses a pre-seeded session to test history-projection ↔
   runtime-id parity (disappear/reappear on refresh).

---

## Part C — Shared compose environment

### C1. `compose.e2e.yml` overlay

```yaml
# docker compose -f compose.local.yml -f compose.e2e.yml up
services:
  mock-llm:
    build: { context: ., dockerfile: Dockerfile.mock-llm }
    ports: ["8090:8090"]
    volumes: ["./tests/e2e/scenarios:/scenarios:ro"]
    environment:
      MOCK_LLM_PORT: 8090
      MOCK_LLM_SCENARIOS_DIR: /scenarios

  cognis:
    environment:
      COGNIS_LOCAL_LLM_BASE_URL: http://mock-llm:8090/v1
      COGNIS_LOCAL_LLM_API_KEY: mock-key
      COGNIS_DEFAULT_MEMORY_BACKEND: none
      COGNIS_DEFAULT_GUARDRAILS_BACKEND: none

  intaris:
    environment:
      ANALYSIS_ENABLED: "false"
      LLM_API_KEY: mock-key
      LLM_BASE_URL: http://mock-llm:8090/v1

  mnemory:
    environment:
      LLM_API_KEY: mock-key
      LLM_BASE_URL: http://mock-llm:8090/v1
      EMBED_API_KEY: mock-key
      EMBED_BASE_URL: http://mock-llm:8090/v1

  seed-e2e:
    profiles: [e2e]
    command: ["python", "/app/scripts/seed_e2e.py"]
```

### C2. Render-layer test hooks

Add `data-testid` / `data-streaming` / `data-kind` / `data-tool-status` attributes to:
- `TimelineItemRenderer.svelte` — `data-kind={item.kind}` on the wrapper div.
- `ChatMessage.svelte` — `data-streaming={item.streaming}` on the message container.
- `ThinkingBlock.svelte` — `data-streaming={live}` on the block container.
- `ToolCallBlock.svelte` — `data-tool-status={item.status}` on the block container.

### C3. `scripts/seed_e2e.py`

Extends `local_compose_seed.py` pattern:
- Creates a deterministic e2e agent with `capabilities={memory_backend:"none",
  guardrails_backend:"none"}`.
- Creates an LLM provider pointing at mock-llm.
- Pre-seeds conversations/sessions for the `refresh-mid-and-post-turn` scenario.
- Idempotent (safe to re-run).

---

## Part D — Static tests (CI feedback loop)

### D1. L2 golden event-stream tests

**pytest** (`tests/e2e/test_timeline_streaming.py`):
- `e2e_stack` fixture: `live_stack` + mock-llm subprocess + seeded e2e agent.
- For each scenario: send trigger via `live_chat_ws`, capture full WS event stream.
- Assert backend-contract invariants (Python).
- Write `tests/e2e/golden/<scenario>.jsonl`.

**vitest** (`ui/src/lib/chat-timeline.golden.test.ts`):
- Load each golden `.jsonl`.
- Replay every event through a real `ChatTimeline` instance.
- Assert client-store invariants:
  - `INV-NO-HANG`: at and after `message_complete`, no item for that turn has `streaming:true`
    or tool_call `status:started`. Checks the snapshot AT `message_complete` (after
    `_finalizeStreamingForTurn` runs synchronously) and all subsequent snapshots.
  - `INV-NO-DUP`: no two items share an `id` at any snapshot.
  - `INV-MONOTONIC-PRESENCE`: an id, once present, never disappears then reappears.
  - `INV-STABLE-ORDERKEY`: `orderKey` for a given id never increases.
  - `INV-FIELD-PRESERVE`: tool_call `arguments`/`evaluation` survive follow-up patches.
  - `INV-FINAL-PRESENCE`: every assistant message and tool_call present during streaming
    must still be present in the final store state after `message_complete`. Catches the
    "message disappears after streaming" bug.
  - `INV-RECONNECT-NO-HANG`: after a `conversation_runtime_snapshot` with
    `has_active_turn:false` is applied, no `streaming:true` / `status:started` items remain.
    Catches the reconnect re-injection bug (stale `active_thinking` re-emitted on reconnect).
  - `INV-REFRESH-NO-DROP`: a refresh (synthetic `conversation_view_refresh` →
    `ChatTimeline.replaceAll`) must not evict an item that was present and unconfirmed-live
    immediately before it. Catches the "message disappears after refresh" bug — `replaceAll`
    preserves streaming / non-terminal-tool / no-seq-sentinel items absent from a refresh
    projection that races event persistence. The e2e capture appends the synthetic refresh
    (full turn items minus the final assistant message) so the golden replay exercises it.

**Routing-faithful golden replay**: the replay's `dispatchEvent` mirrors the page router —
`conversation_runtime_snapshot` → `ChatTimeline.applyRuntimeSnapshot`,
`conversation_view_refresh` → `ChatTimeline.replaceAll`, everything else →
`ChatTimeline.applyEvent`. This catches bugs in paths `applyEvent` alone does not handle.

**Shared invariant libraries**:
- `tests/e2e/invariants.py` — Python backend-contract assertions.
- `ui/src/lib/test-support/timeline-invariants.ts` — TypeScript client-store assertions.

### D2. L3 Playwright browser tests (`ui/e2e/`)

`@playwright/test` against the compose stack:
- Login (seeded admin), open seeded conversation, send scenario trigger.
- Assert DOM: no `LiveDots` after completion; one assistant bubble per phase; no element
  churn (MutationObserver); scroll pinned.
- Thin tier — visual scenarios only.
- `timeline.spec.ts`: spinner/phase/duplicate assertions; `single-phase-stream` also installs
  a MutationObserver asserting the assistant message node is never unmounted mid-stream
  (real flicker check).
- `scroll-stability.spec.ts` (Symptom 4): with `long-streaming-response`, asserts the tail
  stays pinned during a streaming burst (no jitter) and that a manual scroll-up is preserved
  (not yanked back by incoming tokens). Uses `data-testid="timeline-viewport"` /
  `timeline-viewport-content` / `timeline-viewport-scroll-to-bottom` hooks on
  `TimelineViewport`.
- Shared helpers live in `ui/e2e/helpers.ts` (login / scenario inject / send / wait).

---

## Part E — Interactive agent-debugging environment

### E1. One-command live env

```bash
make e2e-up    # docker compose -f compose.local.yml -f compose.e2e.yml up -d --build
make e2e-seed  # run seed-e2e profile
make e2e-down  # docker compose -f compose.local.yml -f compose.e2e.yml down
```

Stack runs on stable ports: Cognis :8080, mock-llm :8090.

### E2. Coding agent drives browser via `@playwright/mcp`

Microsoft `@playwright/mcp` (34k stars, official) is the standard MCP server for agent
browser automation. It exposes click/type/snapshot/console/network tools via MCP.

**Setup** (documented in AGENTS.md, not a code dependency of Cognis):
```json
// .mcp/playwright.json or opencode MCP config
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest", "--browser", "chromium"],
      "env": {}
    }
  }
}
```

**Interactive debug workflow**:
1. `make e2e-up && make e2e-seed`
2. Agent attaches Playwright MCP, navigates to `http://localhost:8080`.
3. Agent logs in (seeded admin credentials).
4. Agent `POST http://localhost:8090/__mock/active` to inject the exact scenario.
5. Agent sends the trigger message in the chat UI.
6. Agent reads accessibility tree + `data-*` hooks + console logs to observe the bug.
7. Agent edits code, rebuilds (`npm run build` or `vite dev`), re-tests.
8. When bug is reproduced, agent saves the injected scenario to `tests/e2e/scenarios/`
   and the captured WS stream to `tests/e2e/golden/` → permanent regression test.

### E3. Fast UI iteration

Two modes (both documented):
- **Build mode** (fidelity): `cd ui && npm run build` → Cognis serves rebuilt assets.
- **Dev mode** (speed): `cd ui && npm run dev -- --port 5173` → Vite dev server proxies
  `/api` to `http://localhost:8080`. CORS already permissive in local.

### E4. Reproduce → promote loop

```bash
# After interactive reproduction:
make e2e-promote SCENARIO=my-new-bug
# Copies /__mock/history last scenario to tests/e2e/scenarios/my-new-bug.yaml
# Copies captured WS stream to tests/e2e/golden/my-new-bug.jsonl
# Adds to the static test suite automatically
```

---

## Part F — Orchestration

### Makefile targets

```makefile
e2e-up:      docker compose -f compose.local.yml -f compose.e2e.yml up -d --build
e2e-seed:    docker compose -f compose.local.yml -f compose.e2e.yml run --rm seed-e2e
e2e-down:    docker compose -f compose.local.yml -f compose.e2e.yml down
e2e-events:  uv run pytest tests/e2e/ -v && cd ui && npm test -- --reporter=verbose
e2e-browser: cd ui && npx playwright test e2e/
e2e-promote: python scripts/promote_e2e_scenario.py $(SCENARIO)
```

### CI

- `e2e-events` (L2): per-PR (fast, deterministic, no browser).
- `e2e-browser` (L3): nightly / on `e2e` label.

---

## Backend registry extensibility

Adding a new backend:

1. Create `cognis/providers/backends/{kind}/{id}.py`.
2. Implement the Provider Protocol.
3. Register with `@register_backend(kind="...", id="...")`.
4. No core edits required.

Near-term planned backends:
- `guardrails: native` — in-Cognis guardrails (no Intaris dependency).
- `session_store: intaris` / `session_store: local` — once session store backend is
  configurable.

Community backends: same pattern, installable as separate packages that register via
Python entrypoints (the registry is shaped to support this without breaking changes).

---

## Intaris compatibility

Intaris requires **no code changes** for the e2e harness:
- `ANALYSIS_ENABLED=false` disables all L2/L3 LLM background work while keeping the
  event store fully functional (this is the documented, intended LLM-free mode per
  Intaris config validation).
- `evaluate` (L1) is never called by Cognis when `guardrails_backend="none"`.
- Event store (`record_events`/`read_events`/`create_session`) works without LLM.

Per-session policy opt-out (for future per-session analysis disable) is deferred.

---

## Mock LLM multi-turn contract

The mock LLM server (`cognis/testing/mock_llm/`) supports multi-turn scenarios where each
`turns` entry corresponds to one LLM call:

- **Turn 0**: initial response (may include tool calls)
- **Turn 1**: response after tool results (continuation)
- etc.

The `turn_index` is derived from the number of assistant messages already in the conversation
history (`_conversation_turn_index`). The scenario is resolved from the **first** user message
(the original trigger), not the last message (which may be a tool result on turn 1+).

**`finish_reason` contract** (critical for multi-phase turns):
- Last step is `tool_call` → `finish_reason="tool_calls"` → agent loop executes tool, bumps
  `assistant_phase_index`, re-invokes LLM (next turn).
- Last step is `text` or `thinking` → `finish_reason="stop"` → turn complete.

Without the correct `finish_reason`, the agent loop treats every response as complete after
the first LLM call, never entering the multi-phase path. This was the root cause of the
harness not reproducing the production multi-phase streaming bug.

## Live WS frame recorder

The WS client (`ui/src/lib/ws/client.ts`) includes a dev-only frame recorder:

```javascript
// Activate via URL: ?recordWs=1
// Or from browser console:
window.__cognisWsRecorder.start()
window.__cognisWsRecorder.download()  // saves ws-recording-<timestamp>.jsonl
window.__cognisWsRecorder.stop()
window.__cognisWsRecorder.count       // number of events recorded
```

The downloaded JSONL file can be placed in `tests/e2e/golden/` and replayed through
`chat-timeline.golden.test.ts` to reproduce production bugs deterministically.

## Post-completion event capture

`capture_ws_events` in `tests/e2e/conftest.py` captures events for `post_completion_window`
seconds (default 3s) after `message_complete`. This includes:
- `conversation_updated` — `has_active_turn: false`
- `turn_settled` — turn state cleared
- `conversation_state_delta` — task/step state updates
- `workflow_step_completed` / `workflow_completed`

These post-completion events are critical for reproducing bugs where items disappear or
stay stuck after the turn completes.

## File map

```
cognis/
  providers/backends/
    __init__.py              # Registry, decorator, resolve_agent_backends
    memory/
      __init__.py
      mnemory.py             # backend_id="mnemory"
      null.py                # backend_id="none" (NullMemoryProvider)
    guardrails/
      __init__.py
      intaris.py             # backend_id="intaris"
      null.py                # backend_id="none" (NoGuardrailsProvider)
  testing/
    mock_llm/
      __init__.py
      server.py              # Starlette app (multi-turn, finish_reason contract)
      scenarios.py           # Scenario loader + SSE rendering
      __main__.py            # python -m cognis.testing.mock_llm
  store/migrations/versions/
    XXXX_add_agent_capabilities.py
  bootstrap.py               # _ensure_agent_capabilities_column()
  config.py                  # default_memory_backend, default_guardrails_backend

tests/e2e/
  __init__.py
  conftest.py                # e2e_stack fixture (post-completion capture)
  invariants.py              # Backend-contract invariant assertions
  scenarios/
    single-phase-stream.yaml
    thinking-multiblock.yaml
    multiphase-thinking-tool-assistant.yaml
    tool-args-then-result.yaml        # multi-turn: text+tool_call → text
    rapid-tokens.yaml
    coding-session-multiphase.yaml    # production-inspired multi-phase
    research-multiphase.yaml          # production-inspired multi-search
    tool-error-recovery.yaml          # production-inspired retry pattern
    long-streaming-response.yaml      # sustained burst
    thinking-then-tools-then-answer.yaml  # thinking + multi-turn
    prod-multiphase-workflow.yaml     # production-shaped: 3 LLM calls, phases 0→1→2
  golden/                    # Written by pytest, read by vitest
  test_timeline_streaming.py # L2 pytest capture (13 scenarios)

ui/
  src/lib/
    test-support/
      timeline-invariants.ts # Client-store invariant assertions (6 invariants)
    chat-timeline.golden.test.ts  # L2 vitest replay
    ws/
      client.ts              # WS client + dev WsFrameRecorder
  e2e/
    playwright.config.ts
    timeline.spec.ts         # L3 Playwright tests (skeleton)

scripts/
  seed_e2e.py                # E2E-specific seed (capability-off agent + scenarios)

compose.e2e.yml              # Overlay for e2e stack
Makefile                     # e2e-* targets
```
