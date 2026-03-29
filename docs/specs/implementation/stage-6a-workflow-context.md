# Stage 6a: Step Input Context Assembly + Iteration Semantics

**Status**: DONE
**Repo**: `cognis`
**Depends on**: Stage 5 (orchestration core — session manager, session cache, context assembler)
**Estimated effort**: 2-3 days

## Objective

Implement the step input context model that controls how context flows
between workflow steps. This is a prerequisite for the workflow engine
(Stage 6) because every step needs its context assembled correctly before
the agent loop can run.

After this stage, the system can:
- assemble context for a workflow step using null/full/summary/last input types
- inject labeled context from previous step outputs or sessions
- continue a step's own session on re-attempt with evaluator feedback appended

## Background

See `docs/specs/14-workflow-engine.md` — "Step Input Context Assembly" and
"StepInputConfig" sections.

### Core rules

1. **Every step creates its own Intaris session.** No session reuse across
   steps. Clean audit boundaries, no contamination.
2. **Iterations within a step continue the same session.** Re-attempt after
   evaluation rejection appends feedback to the step's own session. The agent
   keeps its reasoning.
3. **Default input is `last` from previous step.** Most steps want the
   previous step's completion output. Fresh (`null`) is the exception.

## Deliverables

### 1. StepInputConfig Model

- `cognis/models/workflow.py` (or extend existing workflow models)
  - `StepInputConfig(type, source)`
  - `type`: `"null"` | `"full"` | `"summary"` | `"last"`
  - `source`: `str | list[str] | None`
  - Validation: `full` only accepts single source, not a list
  - Default resolution: if not specified, `type="last"` from previous step;
    if first step, `type="null"`

### 2. Step Context Assembler

- `cognis/core/step_context.py`
  - Given a step definition, task workflow state (accumulated step outputs),
    and the session cache, assemble the initial context for a step.

  - **`null`**: return only step prompt + task description + agent memory
    (delegate to existing ContextAssembler for Mnemory recall)

  - **`full`**: read complete event history from source step's Intaris
    session (via session cache or Intaris provider). Format as conversation
    history messages. Inject before the step prompt.

  - **`summary`**: for each source step, generate an LLM summary of the
    step's session using a cheap model (via LLMProvider, routing policy
    `task_type: "step_summary"`). Inject as labeled system context:
    ```
    <step_context source="plan" type="summary">
    ...summary text...
    </step_context>
    ```

  - **`last`**: for each source step, take the `step_complete` output from
    `task.workflow_state.step_outputs[step_name]`. Inject as labeled context:
    ```
    <step_output source="plan">
    Summary: ...
    Claims: [...]
    Outputs: {...}
    </step_output>
    ```

  - **Multiple sources**: assemble in order with clear provenance labels.

### 3. Iteration Context Handler

- Handle re-attempt within the same step:
  - Do NOT re-assemble input from previous steps
  - Do NOT create a new Intaris session
  - Append evaluator/reviewer feedback as a system message to the existing
    step session:
    ```
    <evaluation_feedback attempt="2">
    Decision: revise
    Feedback: Tests are missing for the login endpoint. The step_complete
    claims "tests pass" but no test files were created.
    </evaluation_feedback>
    ```
  - Resume the agent loop in the same session

### 4. Integration with Existing Context Assembler

- The existing `cognis/core/context.py` (ContextAssembler) handles:
  - Mnemory recall (parallel fetch)
  - Intaris event read (incremental)
  - Intention read
  - System prompt assembly
  - Token budget management

- The new StepContextAssembler should:
  - Call into the existing ContextAssembler for agent memory and system prompt
  - Add the step input context (null/full/summary/last) BEFORE the step prompt
  - Respect the token budget — if `full` input exceeds budget, fall back to
    `summary` automatically with a warning

### 5. Step Output Storage

- After `step_complete` is called, store the output in
  `task.workflow_state.step_outputs`:
  ```python
  workflow_state.step_outputs["plan"] = {
      "summary": "Created 5-step implementation plan...",
      "outputs": {"plan": [...], "criteria": [...]},
      "claims": ["covers auth endpoints", "includes test strategy"],
      "completed_at": "2026-03-29T12:00:00Z",
      "session_id": "step_session_abc",
      "intaris_session_id": "intaris_session_xyz"
  }
  ```
- This is what `last` reads from and what `full`/`summary` use to locate
  the source step's Intaris session.

## Acceptance Criteria

- [x] `StepInputConfig` model validates correctly (full rejects list source)
- [x] Default input resolution works (last from previous, null for first step)
- [x] `null` input: step gets only prompt + task description + agent memory
- [x] `full` input: complete event history from source step injected as context
- [x] `summary` input: LLM-generated summary injected with provenance labels
- [x] `last` input: step_complete output injected with provenance labels
- [x] Multiple sources assembled in order with correct labels
- [x] `full` auto-fallback to `summary` when context budget exceeded
- [x] Iteration: same session continues with feedback appended (no new session)
- [x] Iteration: no input re-assembly on re-attempt
- [x] Step output stored in workflow_state after step_complete
- [x] Step output includes session references for full/summary lookups
- [x] Token budget respected across all input types
- [x] Unit tests for each input type
- [x] Unit tests for iteration feedback injection
- [x] Unit tests for default resolution
- [x] `ruff check` and `mypy` clean

## Key References

- `docs/specs/14-workflow-engine.md` — StepInputConfig, Step Input Context Assembly, iteration semantics
- `docs/specs/03-session-model.md` — session/Intaris mapping
- `docs/specs/01-architecture.md` — session cache architecture
