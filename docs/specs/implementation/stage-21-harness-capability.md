# Stage 21: Harness Capability Parity

## Status

PLANNED

## Goal

Close the largest harness capability gaps that currently keep Cognis below the
best coding/research agent harnesses.

This stage implements Track B from
[`../23-harness-stabilization.md`](../23-harness-stabilization.md):

- parallel tool execution for read-only batches
- repository-scale search and glob quality
- background shell lifecycle support
- finish-reason handling, token-aware truncation, and real step timeouts
- compaction and evaluator robustness

## Dependencies

- `docs/specs/06-tool-system.md`
- `docs/specs/13-nfr-operations.md`
- `docs/specs/14-workflow-engine.md`
- `docs/specs/23-harness-stabilization.md`
- Stage 20 correctness stabilization complete

## Scope

### In Scope

- parallel execution for `read_only=True` tool batches
- `ripgrep` / `fd` backed search with graceful fallback
- background bash sessions and process-group cleanup
- finish-reason propagation and continuation behavior
- token-aware truncation budgets
- enforced step-level timeout
- compaction fallback on no-progress sessions
- improved workflow passive-stop and evaluator behavior

### Out of Scope

- typed deliverables
- step profiles
- workflow parallel branches or sub-workflows

## Deliverables

### 1. Parallel tool execution

- parallel scheduling for read-only tools emitted in the same assistant turn
- deterministic result ordering in transcripts
- serial handling for orchestration/controller tools

### 2. Search and shell parity

- `ripgrep`-backed grep and filtered glob with graceful Python fallback
- default skip directories for repository junk trees
- background `bash` sessions with `shell_id`, polling, and termination
- process-group cleanup on timeout and cancellation

### 3. LLM termination and timeout semantics

- capture and act on finish/stop reasons
- continuation on `length` truncation
- structured error path for `content_filter`
- true step-level timeout enforcement

### 4. Token-aware budgeting

- replace char-count truncation with token-aware truncation
- keep one truncation boundary, not competing truncators
- surface estimated-vs-actual token accounting when exact tokenization is not
  available

### 5. Workflow robustness improvements

- compaction fallback when user-turn split makes no progress
- more forgiving passive-stop reprompt behavior
- evaluator no longer silently approves on timeout/malformed output

### 6. Tests and telemetry

- unit/integration coverage for parallel batches, shell lifecycle, and finish
  reasons
- metrics for batch size, backend selection, timeout, truncation, and
  compaction fallback

## Suggested Work Breakdown

### Workstream A: Parallel execution

Files likely touched:

- `cognis/core/agent_loop.py`
- `cognis/core/tool_router.py`
- `cognis/core/harness_guards.py`

Tasks:

1. Split tool batches into parallel-safe and serial groups.
2. Preserve transcript ordering and consistent guard behavior.
3. Update tests that currently assume sequential dispatch.

### Workstream B: Search and shell

Files likely touched:

- `cognis/tools/executor/search.py`
- `cognis/tools/executor/shell.py`
- `cognis/tools/executor/definitions.py`

Tasks:

1. Use `rg`/`fd` when available.
2. Add background shell lifecycle tools.
3. Fix timeout cleanup and output truncation behavior.

### Workstream C: LLM and workflow semantics

Files likely touched:

- `cognis/core/agent_loop.py`
- `cognis/core/truncation.py`
- `cognis/providers/llm/litellm.py`
- `cognis/core/step_evaluator.py`
- `cognis/core/compaction.py`

Tasks:

1. Plumb finish reasons through the streaming stack.
2. Enforce step-level timeout.
3. Replace char-based truncation with token-aware truncation.
4. Tighten evaluator and passive-stop behavior.

## Acceptance Criteria

- read-only tool batches execute in parallel and remain transcript-safe
- grep/glob are fast and repository-aware when `rg`/`fd` exist
- shell commands can run in the background and be polled/cancelled safely
- `length` truncation is detected and continued
- compaction on tool-heavy sessions always makes progress or degrades cleanly
